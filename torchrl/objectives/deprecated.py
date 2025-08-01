# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Number

import numpy as np
import torch
from tensordict import TensorDict, TensorDictBase, TensorDictParams
from tensordict.nn import composite_lp_aggregate, dispatch, TensorDictModule
from tensordict.utils import NestedKey
from torch import Tensor

from torchrl.data.tensor_specs import Composite
from torchrl.envs.utils import ExplorationType, set_exploration_type, step_mdp
from torchrl.objectives import default_value_kwargs, distance_loss, ValueEstimators
from torchrl.objectives.common import LossModule
from torchrl.objectives.utils import (
    _cache_values,
    _GAMMA_LMBDA_DEPREC_ERROR,
    _reduce,
    _vmap_func,
)
from torchrl.objectives.value import TD0Estimator, TD1Estimator, TDLambdaEstimator


class REDQLoss_deprecated(LossModule):
    """REDQ Loss module.

    REDQ (RANDOMIZED ENSEMBLED DOUBLE Q-LEARNING: LEARNING FAST WITHOUT A MODEL
    https://openreview.net/pdf?id=AY8zfZm0tDd) generalizes the idea of using an ensemble of Q-value functions to
    train a SAC-like algorithm.

    Args:
        actor_network (TensorDictModule): the actor to be trained
        qvalue_network (TensorDictModule): a single Q-value network that will
            be multiplied as many times as needed.
            If a single instance of `qvalue_network` is provided, it will be duplicated ``num_qvalue_nets``
            times. If a list of modules is passed, their
            parameters will be stacked unless they share the same identity (in which case
            the original parameter will be expanded).

            .. warning:: When a list of parameters if passed, it will __not__ be compared against the policy parameters
              and all the parameters will be considered as untied.

    Keyword Args:
        num_qvalue_nets (int, optional): Number of Q-value networks to be trained.
            Default is ``10``.
        sub_sample_len (int, optional): number of Q-value networks to be
            subsampled to evaluate the next state value
            Default is ``2``.
        loss_function (str, optional): loss function to be used for the Q-value.
            Can be one of  ``"smooth_l1"``, ``"l2"``,
            ``"l1"``, Default is ``"smooth_l1"``.
        alpha_init (:obj:`float`, optional): initial entropy multiplier.
            Default is ``1.0``.
        min_alpha (:obj:`float`, optional): min value of alpha.
            Default is ``0.1``.
        max_alpha (:obj:`float`, optional): max value of alpha.
            Default is ``10.0``.
        action_spec (TensorSpec, optional): the action tensor spec. If not provided
            and the target entropy is ``"auto"``, it will be retrieved from
            the actor.
        fixed_alpha (bool, optional): whether alpha should be trained to match
            a target entropy. Default is ``False``.
        target_entropy (Union[str, Number], optional): Target entropy for the
            stochastic policy. Default is "auto".
        delay_qvalue (bool, optional): Whether to separate the target Q value
            networks from the Q value networks used
            for data collection. Default is ``False``.
        gSDE (bool, optional): Knowing if gSDE is used is necessary to create
            random noise variables.
            Default is ``False``.
        priority_key (str, optional): [Deprecated] Key where to write the priority value
            for prioritized replay buffers. Default is
            ``"td_error"``.
        separate_losses (bool, optional): if ``True``, shared parameters between
            policy and critic will only be trained on the policy loss.
            Defaults to ``False``, i.e., gradients are propagated to shared
            parameters for both policy and critic losses.
        reduction (str, optional): Specifies the reduction to apply to the output:
            ``"none"`` | ``"mean"`` | ``"sum"``. ``"none"``: no reduction will be applied,
            ``"mean"``: the sum of the output will be divided by the number of
            elements in the output, ``"sum"``: the output will be summed. Default: ``"mean"``.
        deactivate_vmap (bool, optional): whether to deactivate vmap calls and replace them with a plain for loop.
            Defaults to ``False``.
    """

    @dataclass
    class _AcceptedKeys:
        """Maintains default values for all configurable tensordict keys.

        This class defines which tensordict keys can be set using '.set_keys(key_name=key_value)' and their
        default values.

        Attributes:
            action (NestedKey): The input tensordict key where the action is expected.
                Defaults to ``"advantage"``.
            value (NestedKey): The input tensordict key where the state value is expected.
                Will be used for the underlying value estimator. Defaults to ``"state_value"``.
            state_action_value (NestedKey): The input tensordict key where the
                state action value is expected.  Defaults to ``"state_action_value"``.
            log_prob (NestedKey): The input tensordict key where the log probability is expected.
                Defaults to ``"_log_prob"``.
            priority (NestedKey): The input tensordict key where the target priority is written to.
                Defaults to ``"td_error"``.
            reward (NestedKey): The input tensordict key where the reward is expected.
                Will be used for the underlying value estimator. Defaults to ``"reward"``.
            done (NestedKey): The key in the input TensorDict that indicates
                whether a trajectory is done. Will be used for the underlying value estimator.
                Defaults to ``"done"``.
            terminated (NestedKey): The key in the input TensorDict that indicates
                whether a trajectory is terminated. Will be used for the underlying value estimator.
                Defaults to ``"terminated"``.
        """

        action: NestedKey = "action"
        state_action_value: NestedKey = "state_action_value"
        value: NestedKey = "state_value"
        log_prob: NestedKey | None = None
        priority: NestedKey = "td_error"
        reward: NestedKey = "reward"
        done: NestedKey = "done"
        terminated: NestedKey = "terminated"

        def __post_init__(self):
            if self.log_prob is None:
                if composite_lp_aggregate(nowarn=True):
                    self.log_prob = "sample_log_prob"
                else:
                    self.log_prob = "action_log_prob"

    tensor_keys: _AcceptedKeys
    default_keys = _AcceptedKeys
    delay_actor: bool = False
    default_value_estimator = ValueEstimators.TD0

    actor_network: TensorDictModule
    qvalue_network: TensorDictModule
    actor_network_params: TensorDictParams
    qvalue_network_params: TensorDictParams
    target_actor_network_params: TensorDictParams
    target_qvalue_network_params: TensorDictParams

    def __init__(
        self,
        actor_network: TensorDictModule,
        qvalue_network: TensorDictModule | list[TensorDictModule],
        *,
        num_qvalue_nets: int = 10,
        sub_sample_len: int = 2,
        loss_function: str = "smooth_l1",
        alpha_init: float = 1.0,
        min_alpha: float = 0.1,
        max_alpha: float = 10.0,
        action_spec=None,
        fixed_alpha: bool = False,
        target_entropy: str | Number = "auto",
        delay_qvalue: bool = True,
        gSDE: bool = False,
        gamma: float | None = None,
        priority_key: str = None,
        separate_losses: bool = False,
        reduction: str = None,
        deactivate_vmap: bool = False,
    ):
        self._in_keys = None
        self._out_keys = None
        if reduction is None:
            reduction = "mean"
        super().__init__()
        self._set_deprecated_ctor_keys(priority_key=priority_key)

        self.deactivate_vmap = deactivate_vmap

        self.convert_to_functional(
            actor_network,
            "actor_network",
            create_target_params=self.delay_actor,
        )
        if separate_losses:
            # we want to make sure there are no duplicates in the params: the
            # params of critic must be refs to actor if they're shared
            policy_params = list(actor_network.parameters())
        else:
            policy_params = None
        # let's make sure that actor_network has `return_log_prob` set to True
        self.actor_network.return_log_prob = True

        self.delay_qvalue = delay_qvalue
        self.convert_to_functional(
            qvalue_network,
            "qvalue_network",
            expand_dim=num_qvalue_nets,
            create_target_params=self.delay_qvalue,
            compare_against=policy_params,
        )
        self.num_qvalue_nets = num_qvalue_nets
        self.sub_sample_len = max(1, min(sub_sample_len, num_qvalue_nets - 1))
        self.loss_function = loss_function

        try:
            device = next(self.parameters()).device
        except AttributeError:
            device = getattr(torch, "get_default_device", lambda: torch.device("cpu"))()

        self.register_buffer("alpha_init", torch.as_tensor(alpha_init, device=device))
        self.register_buffer(
            "min_log_alpha", torch.as_tensor(min_alpha, device=device).log()
        )
        self.register_buffer(
            "max_log_alpha", torch.as_tensor(max_alpha, device=device).log()
        )
        self.fixed_alpha = fixed_alpha
        if fixed_alpha:
            self.register_buffer(
                "log_alpha", torch.as_tensor(math.log(alpha_init), device=device)
            )
        else:
            self.register_parameter(
                "log_alpha",
                torch.nn.Parameter(
                    torch.as_tensor(math.log(alpha_init), device=device)
                ),
            )

        self._target_entropy = target_entropy
        self._action_spec = action_spec
        self.target_entropy_buffer = None
        self.gSDE = gSDE
        self._make_vmap()
        self.reduction = reduction

        if gamma is not None:
            raise TypeError(_GAMMA_LMBDA_DEPREC_ERROR)

    def _make_vmap(self):
        self._vmap_qvalue_networkN0 = _vmap_func(
            self.qvalue_network, (None, 0), pseudo_vmap=self.deactivate_vmap
        )

    @property
    def target_entropy(self):
        target_entropy = self.target_entropy_buffer
        if target_entropy is None:
            delattr(self, "target_entropy_buffer")
            target_entropy = self._target_entropy
            action_spec = self._action_spec
            actor_network = self.actor_network
            device = next(self.parameters()).device
            if target_entropy == "auto":
                action_spec = (
                    action_spec
                    if action_spec is not None
                    else getattr(actor_network, "spec", None)
                )
                if action_spec is None:
                    raise RuntimeError(
                        "Cannot infer the dimensionality of the action. Consider providing "
                        "the target entropy explicitly or provide the spec of the "
                        "action tensor in the actor network."
                    )
                if not isinstance(action_spec, Composite):
                    action_spec = Composite({self.tensor_keys.action: action_spec})
                target_entropy = -float(
                    np.prod(action_spec[self.tensor_keys.action].shape)
                )
            self.register_buffer(
                "target_entropy_buffer", torch.as_tensor(target_entropy, device=device)
            )
            return self.target_entropy_buffer
        return target_entropy

    def _forward_value_estimator_keys(self, **kwargs) -> None:
        if self._value_estimator is not None:
            self._value_estimator.set_keys(
                value=self.tensor_keys.value,
                reward=self.tensor_keys.reward,
                done=self.tensor_keys.done,
                terminated=self.tensor_keys.terminated,
            )
        self._set_in_keys()

    @property
    def alpha(self):
        with torch.no_grad():
            # keep alpha is a reasonable range
            alpha = self.log_alpha.clamp(self.min_log_alpha, self.max_log_alpha).exp()
        return alpha

    def _set_in_keys(self):
        keys = [
            self.tensor_keys.action,
            ("next", self.tensor_keys.reward),
            ("next", self.tensor_keys.done),
            ("next", self.tensor_keys.terminated),
            *self.actor_network.in_keys,
            *[("next", key) for key in self.actor_network.in_keys],
            *self.qvalue_network.in_keys,
        ]
        self._in_keys = list(set(keys))

    @property
    def in_keys(self):
        if self._in_keys is None:
            self._set_in_keys()
        return self._in_keys

    @in_keys.setter
    def in_keys(self, values):
        self._in_keys = values

    @property
    def out_keys(self):
        if self._out_keys is None:
            keys = ["loss_actor", "loss_qvalue", "loss_alpha", "alpha", "entropy"]
            self._out_keys = keys
        return self._out_keys

    @out_keys.setter
    def out_keys(self, values):
        self._out_keys = values

    @dispatch
    def forward(self, tensordict: TensorDictBase) -> TensorDictBase:
        loss_actor, sample_log_prob = self._actor_loss(tensordict)

        loss_qval = self._qvalue_loss(tensordict)
        loss_alpha = self._loss_alpha(sample_log_prob)
        if not loss_qval.shape == loss_actor.shape:
            raise RuntimeError(
                f"QVal and actor loss have different shape: {loss_qval.shape} and {loss_actor.shape}"
            )
        td_out = TensorDict(
            {
                "loss_actor": loss_actor,
                "loss_qvalue": loss_qval,
                "loss_alpha": loss_alpha,
                "alpha": self.alpha,
                "entropy": -sample_log_prob.detach().mean(),
            },
            [],
        )
        td_out = td_out.named_apply(
            lambda name, value: _reduce(value, reduction=self.reduction)
            if name.startswith("loss_")
            else value,
            batch_size=[],
        )
        self._clear_weakrefs(
            tensordict,
            td_out,
            "actor_network_params",
            "qvalue_network_params",
            "target_actor_network_params",
            "target_qvalue_network_params",
        )
        return td_out

    @property
    @_cache_values
    def _cached_detach_qvalue_network_params(self):
        return self.qvalue_network_params.detach()

    def _actor_loss(self, tensordict: TensorDictBase) -> tuple[Tensor, Tensor]:
        obs_keys = self.actor_network.in_keys
        tensordict_clone = tensordict.select(*obs_keys, strict=False)
        with set_exploration_type(
            ExplorationType.RANDOM
        ), self.actor_network_params.to_module(self.actor_network):
            self.actor_network(tensordict_clone)

        tensordict_expand = self._vmap_qvalue_networkN0(
            tensordict_clone.select(*self.qvalue_network.in_keys, strict=False),
            self._cached_detach_qvalue_network_params,
        )
        state_action_value = tensordict_expand.get(
            self.tensor_keys.state_action_value
        ).squeeze(-1)
        loss_actor = -(
            state_action_value
            - self.alpha * tensordict_clone.get(self.tensor_keys.log_prob).squeeze(-1)
        )
        return loss_actor, tensordict_clone.get(self.tensor_keys.log_prob)

    def _qvalue_loss(self, tensordict: TensorDictBase) -> Tensor:
        tensordict_save = tensordict

        obs_keys = self.actor_network.in_keys
        tensordict = tensordict.select(
            "next", *obs_keys, self.tensor_keys.action, strict=False
        ).clone(False)

        selected_models_idx = torch.randperm(self.num_qvalue_nets)[
            : self.sub_sample_len
        ].sort()[0]
        with torch.no_grad():
            selected_q_params = self.target_qvalue_network_params[selected_models_idx]

            next_td = step_mdp(tensordict).select(
                *self.actor_network.in_keys, strict=False
            )  # next_observation ->
            # observation
            # select pseudo-action
            with set_exploration_type(
                ExplorationType.RANDOM
            ), self.target_actor_network_params.to_module(self.actor_network):
                self.actor_network(next_td)
            sample_log_prob = next_td.get(self.tensor_keys.log_prob)
            # get q-values
            next_td = self._vmap_qvalue_networkN0(
                next_td,
                selected_q_params,
            )
            state_action_value = next_td.get(self.tensor_keys.state_action_value)
            if (
                state_action_value.shape[-len(sample_log_prob.shape) :]
                != sample_log_prob.shape
            ):
                sample_log_prob = sample_log_prob.unsqueeze(-1)
            next_state_value = (
                next_td.get(self.tensor_keys.state_action_value)
                - self.alpha * sample_log_prob
            )
            next_state_value = next_state_value.min(0)[0]

        tensordict.set(("next", self.tensor_keys.value), next_state_value)
        target_value = self.value_estimator.value_estimate(tensordict).squeeze(-1)
        tensordict_expand = self._vmap_qvalue_networkN0(
            tensordict.select(*self.qvalue_network.in_keys, strict=False),
            self.qvalue_network_params,
        )
        pred_val = tensordict_expand.get(self.tensor_keys.state_action_value).squeeze(
            -1
        )
        td_error = abs(pred_val - target_value)
        loss_qval = distance_loss(
            pred_val,
            target_value.expand_as(pred_val),
            loss_function=self.loss_function,
        )
        tensordict_save.set("td_error", td_error.detach().max(0)[0])
        return loss_qval

    def _loss_alpha(self, log_pi: Tensor) -> Tensor:
        if torch.is_grad_enabled() and not log_pi.requires_grad:
            raise RuntimeError(
                "expected log_pi to require gradient for the alpha loss)"
            )
        if self.target_entropy is not None:
            # we can compute this loss even if log_alpha is not a parameter
            alpha_loss = -self.log_alpha.clamp(
                self.min_log_alpha, self.max_log_alpha
            ).exp() * (log_pi.detach() + self.target_entropy)
        else:
            # placeholder
            alpha_loss = torch.zeros_like(log_pi)
        return alpha_loss

    def make_value_estimator(self, value_type: ValueEstimators = None, **hyperparams):
        if value_type is None:
            value_type = self.default_value_estimator
        self.value_type = value_type
        hp = dict(default_value_kwargs(value_type))
        if hasattr(self, "gamma"):
            hp["gamma"] = self.gamma
        hp.update(hyperparams)
        # we do not need a value network bc the next state value is already passed
        if value_type == ValueEstimators.TD1:
            self._value_estimator = TD1Estimator(value_network=None, **hp)
        elif value_type == ValueEstimators.TD0:
            self._value_estimator = TD0Estimator(value_network=None, **hp)
        elif value_type == ValueEstimators.GAE:
            raise NotImplementedError(
                f"Value type {value_type} it not implemented for loss {type(self)}."
            )
        elif value_type == ValueEstimators.TDLambda:
            self._value_estimator = TDLambdaEstimator(value_network=None, **hp)
        else:
            raise NotImplementedError(f"Unknown value type {value_type}")
        tensor_keys = {
            "value": self.tensor_keys.value,
            "reward": self.tensor_keys.reward,
            "done": self.tensor_keys.done,
            "terminated": self.tensor_keys.terminated,
        }
        self._value_estimator.set_keys(**tensor_keys)


class DoubleREDQLoss_deprecated(REDQLoss_deprecated):
    """[Deprecated] Class for delayed target-REDQ (which should be the default behavior)."""

    delay_qvalue: bool = True

    actor_network: TensorDictModule
    qvalue_network: TensorDictModule
    actor_network_params: TensorDictParams
    qvalue_network_params: TensorDictParams
    target_actor_network_params: TensorDictParams
    target_qvalue_network_params: TensorDictParams
