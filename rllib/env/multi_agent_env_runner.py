from collections import defaultdict
from functools import partial
import math
import logging
import time
from typing import Collection, DefaultDict, Dict, List, Optional, Union

import gymnasium as gym

import ray
from ray.rllib.algorithms.algorithm_config import AlgorithmConfig
from ray.rllib.callbacks.utils import make_callback
from ray.rllib.core import (
    COMPONENT_ENV_TO_MODULE_CONNECTOR,
    COMPONENT_MODULE_TO_ENV_CONNECTOR,
    COMPONENT_RL_MODULE,
)
from ray.rllib.core.columns import Columns
from ray.rllib.core.rl_module.multi_rl_module import MultiRLModule, MultiRLModuleSpec
from ray.rllib.env.env_context import EnvContext
from ray.rllib.env.env_runner import EnvRunner, ENV_STEP_FAILURE
from ray.rllib.env.multi_agent_env import MultiAgentEnv
from ray.rllib.env.multi_agent_episode import MultiAgentEpisode
from ray.rllib.env.utils import _gym_env_creator
from ray.rllib.utils import force_list
from ray.rllib.utils.annotations import override
from ray.rllib.utils.checkpoints import Checkpointable
from ray.rllib.utils.deprecation import Deprecated
from ray.rllib.utils.framework import get_device, try_import_torch
from ray.rllib.utils.metrics import (
    EPISODE_DURATION_SEC_MEAN,
    EPISODE_LEN_MAX,
    EPISODE_LEN_MEAN,
    EPISODE_LEN_MIN,
    EPISODE_RETURN_MAX,
    EPISODE_RETURN_MEAN,
    EPISODE_RETURN_MIN,
    NUM_AGENT_STEPS_SAMPLED,
    NUM_AGENT_STEPS_SAMPLED_LIFETIME,
    NUM_ENV_STEPS_SAMPLED,
    NUM_ENV_STEPS_SAMPLED_LIFETIME,
    NUM_EPISODES,
    NUM_EPISODES_LIFETIME,
    NUM_MODULE_STEPS_SAMPLED,
    NUM_MODULE_STEPS_SAMPLED_LIFETIME,
    TIME_BETWEEN_SAMPLING,
    WEIGHTS_SEQ_NO,
)
from ray.rllib.utils.metrics.metrics_logger import MetricsLogger
from ray.rllib.utils.pre_checks.env import check_multiagent_environments
from ray.rllib.utils.typing import EpisodeID, ModelWeights, ResultDict, StateDict
from ray.tune.registry import ENV_CREATOR, _global_registry
from ray.util.annotations import PublicAPI

torch, _ = try_import_torch()
logger = logging.getLogger("ray.rllib")


# TODO (sven): As soon as RolloutWorker is no longer supported, make `EnvRunner` itself
#  a Checkpointable. Currently, only some of its subclasses are Checkpointables.
@PublicAPI(stability="alpha")
class MultiAgentEnvRunner(EnvRunner, Checkpointable):
    """The genetic environment runner for the multi-agent case."""

    @override(EnvRunner)
    def __init__(self, config: AlgorithmConfig, **kwargs):
        """Initializes a MultiAgentEnvRunner instance.

        Args:
            config: An `AlgorithmConfig` object containing all settings needed to
                build this `EnvRunner` class.
        """
        super().__init__(config=config)

        # Raise an Error, if the provided config is not a multi-agent one.
        if not self.config.is_multi_agent:
            raise ValueError(
                f"Cannot use this EnvRunner class ({type(self).__name__}), if your "
                "setup is not multi-agent! Try adding multi-agent information to your "
                "AlgorithmConfig via calling the `config.multi_agent(policies=..., "
                "policy_mapping_fn=...)`."
            )

        # Get the worker index on which this instance is running.
        self.worker_index: int = kwargs.get("worker_index")
        self.tune_trial_id: str = kwargs.get("tune_trial_id")

        # Set up all metrics-related structures and counters.
        self.metrics: Optional[MetricsLogger] = None
        self._setup_metrics()

        # Create our callbacks object.
        self._callbacks = [cls() for cls in force_list(self.config.callbacks_class)]

        # Set device.
        self._device = get_device(
            self.config,
            0 if not self.worker_index else self.config.num_gpus_per_env_runner,
        )

        # Create the vectorized gymnasium env.
        self.env: Optional[gym.Wrapper] = None
        self.num_envs: int = 0
        self.make_env()

        # Create the env-to-module connector pipeline.
        self._env_to_module = self.config.build_env_to_module_connector(
            self.env.unwrapped, device=self._device
        )
        # Cached env-to-module results taken at the end of a `_sample_timesteps()`
        # call to make sure the final observation (before an episode cut) gets properly
        # processed (and maybe postprocessed and re-stored into the episode).
        # For example, if we had a connector that normalizes observations and directly
        # re-inserts these new obs back into the episode, the last observation in each
        # sample call would NOT be processed, which could be very harmful in cases,
        # in which value function bootstrapping of those (truncation) observations is
        # required in the learning step.
        self._cached_to_module = None

        # Construct the MultiRLModule.
        self.module: Optional[MultiRLModule] = None
        self.make_module()

        # Create the module-to-env connector pipeline.
        self._module_to_env = self.config.build_module_to_env_connector(
            self.env.unwrapped
        )

        self._needs_initial_reset: bool = True
        self._episode: Optional[MultiAgentEpisode] = None
        self._shared_data = None

        self._weights_seq_no: int = 0

        # Measures the time passed between returning from `sample()`
        # and receiving the next `sample()` request from the user.
        self._time_after_sampling = None

    @override(EnvRunner)
    def sample(
        self,
        *,
        num_timesteps: int = None,
        num_episodes: int = None,
        explore: bool = None,
        random_actions: bool = False,
        force_reset: bool = False,
    ) -> List[MultiAgentEpisode]:
        """Runs and returns a sample (n timesteps or m episodes) on the env(s).

        Args:
            num_timesteps: The number of timesteps to sample during this call.
                Note that only one of `num_timetseps` or `num_episodes` may be provided.
            num_episodes: The number of episodes to sample during this call.
                Note that only one of `num_timetseps` or `num_episodes` may be provided.
            explore: If True, will use the RLModule's `forward_exploration()`
                method to compute actions. If False, will use the RLModule's
                `forward_inference()` method. If None (default), will use the `explore`
                boolean setting from `self.config` passed into this EnvRunner's
                constructor. You can change this setting in your config via
                `config.env_runners(explore=True|False)`.
            random_actions: If True, actions will be sampled randomly (from the action
                space of the environment). If False (default), actions or action
                distribution parameters are computed by the RLModule.
            force_reset: Whether to force-reset all (vector) environments before
                sampling. Useful if you would like to collect a clean slate of new
                episodes via this call. Note that when sampling n episodes
                (`num_episodes != None`), this is fixed to True.

        Returns:
            A list of `MultiAgentEpisode` instances, carrying the sampled data.
        """
        assert not (num_timesteps is not None and num_episodes is not None)

        # Log time between `sample()` requests.
        if self._time_after_sampling is not None:
            self.metrics.log_value(
                key=TIME_BETWEEN_SAMPLING,
                value=time.perf_counter() - self._time_after_sampling,
            )

        # If no execution details are provided, use the config to try to infer the
        # desired timesteps/episodes to sample and the exploration behavior.
        if explore is None:
            explore = self.config.explore
        if num_timesteps is None and num_episodes is None:
            if self.config.batch_mode == "truncate_episodes":
                num_timesteps = self.config.get_rollout_fragment_length(
                    worker_index=self.worker_index,
                )
            else:
                num_episodes = 1

        # Sample n timesteps.
        if num_timesteps is not None:
            samples = self._sample_timesteps(
                num_timesteps=num_timesteps,
                explore=explore,
                random_actions=random_actions,
                force_reset=force_reset,
            )
        # Sample m episodes.
        else:
            samples = self._sample_episodes(
                num_episodes=num_episodes,
                explore=explore,
                random_actions=random_actions,
            )

        # Make the `on_sample_end` callback.
        make_callback(
            "on_sample_end",
            callbacks_objects=self._callbacks,
            callbacks_functions=self.config.callbacks_on_sample_end,
            kwargs=dict(
                env_runner=self,
                metrics_logger=self.metrics,
                samples=samples,
            ),
        )

        self._time_after_sampling = time.perf_counter()

        return samples

    def _sample_timesteps(
        self,
        num_timesteps: int,
        explore: bool,
        random_actions: bool = False,
        force_reset: bool = False,
    ) -> List[MultiAgentEpisode]:
        """Helper method to sample n timesteps.

        Args:
            num_timesteps: int. Number of timesteps to sample during rollout.
            explore: boolean. If in exploration or inference mode. Exploration
                mode might for some algorithms provide extza model outputs that
                are redundant in inference mode.
            random_actions: boolean. If actions should be sampled from the action
                space. In default mode (i.e. `False`) we sample actions frokm the
                policy.

        Returns:
            `Lists of `MultiAgentEpisode` instances, carrying the collected sample data.
        """
        done_episodes_to_return: List[MultiAgentEpisode] = []

        # Have to reset the env.
        if force_reset or self._needs_initial_reset:
            # Create n new episodes and make the `on_episode_created` callbacks.
            self._episode = self._new_episode()
            self._make_on_episode_callback("on_episode_created")

            # Erase all cached ongoing episodes (these will never be completed and
            # would thus never be returned/cleaned by `get_metrics` and cause a memory
            # leak).
            self._ongoing_episodes_for_metrics.clear()

            # Try resetting the environment.
            # TODO (simon): Check, if we need here the seed from the config.
            obs, infos = self._try_env_reset()

            self._cached_to_module = None

            # Call `on_episode_start()` callbacks.
            self._make_on_episode_callback("on_episode_start")

            # We just reset the env. Don't have to force this again in the next
            # call to `self._sample_timesteps()`.
            self._needs_initial_reset = False

            # Set the initial observations in the episodes.
            self._episode.add_env_reset(observations=obs, infos=infos)

            self._shared_data = {
                "agent_to_module_mapping_fn": self.config.policy_mapping_fn,
            }

        # Loop through timesteps.
        ts = 0

        while ts < num_timesteps:
            # Act randomly.
            if random_actions:
                # Only act (randomly) for those agents that had an observation.
                to_env = {
                    Columns.ACTIONS: [
                        {
                            aid: self.env.unwrapped.get_action_space(aid).sample()
                            for aid in self._episode.get_agents_to_act()
                        }
                    ]
                }
            # Compute an action using the RLModule.
            else:
                # Env-to-module connector.
                to_module = self._cached_to_module or self._env_to_module(
                    rl_module=self.module,
                    episodes=[self._episode],
                    explore=explore,
                    shared_data=self._shared_data,
                    metrics=self.metrics,
                )
                self._cached_to_module = None

                # MultiRLModule forward pass: Explore or not.
                if explore:
                    env_steps_lifetime = (
                        self.metrics.peek(NUM_ENV_STEPS_SAMPLED_LIFETIME, default=0)
                        + self.metrics.peek(NUM_ENV_STEPS_SAMPLED, default=0)
                    ) * (self.config.num_env_runners or 1)
                    to_env = self.module.forward_exploration(
                        to_module, t=env_steps_lifetime
                    )
                else:
                    to_env = self.module.forward_inference(to_module)

                # Module-to-env connector.
                to_env = self._module_to_env(
                    rl_module=self.module,
                    batch=to_env,
                    episodes=[self._episode],
                    explore=explore,
                    shared_data=self._shared_data,
                    metrics=self.metrics,
                )

            # Extract the (vectorized) actions (to be sent to the env) from the
            # module/connector output. Note that these actions are fully ready (e.g.
            # already unsquashed/clipped) to be sent to the environment) and might not
            # be identical to the actions produced by the RLModule/distribution, which
            # are the ones stored permanently in the episode objects.
            actions = to_env.pop(Columns.ACTIONS)
            actions_for_env = to_env.pop(Columns.ACTIONS_FOR_ENV, actions)

            # Try stepping the environment.
            # TODO (sven): [0] = actions is vectorized, but env is NOT a vector Env.
            #  Support vectorized multi-agent envs.
            results = self._try_env_step(actions_for_env[0])
            # If any failure occurs during stepping -> Throw away all data collected
            # thus far and restart sampling procedure.
            if results == ENV_STEP_FAILURE:
                return self._sample_timesteps(
                    num_timesteps=num_timesteps,
                    explore=explore,
                    random_actions=random_actions,
                    force_reset=True,
                )
            obs, rewards, terminateds, truncateds, infos = results

            # TODO (sven): This simple approach to re-map `to_env` from a
            #  dict[col, List[MADict]] to a dict[agentID, MADict] would not work for
            #  a vectorized env.
            extra_model_outputs = defaultdict(dict)
            for col, ma_dict_list in to_env.items():
                # TODO (sven): Support vectorized MA env.
                ma_dict = ma_dict_list[0]
                for agent_id, val in ma_dict.items():
                    extra_model_outputs[agent_id][col] = val
                    extra_model_outputs[agent_id][WEIGHTS_SEQ_NO] = self._weights_seq_no
            extra_model_outputs = dict(extra_model_outputs)

            # Record the timestep in the episode instance.
            self._episode.add_env_step(
                obs,
                actions[0],
                rewards,
                infos=infos,
                terminateds=terminateds,
                truncateds=truncateds,
                extra_model_outputs=extra_model_outputs,
            )

            ts += self._increase_sampled_metrics(self.num_envs, obs, self._episode)

            # Make the `on_episode_step` callback (before finalizing the episode
            # object).
            self._make_on_episode_callback("on_episode_step")

            # Episode is done for all agents. Wrap up the old one and create a new
            # one (and reset it) to continue.
            if self._episode.is_done:
                # We have to perform an extra env-to-module pass here, just in case
                # the user's connector pipeline performs (permanent) transforms
                # on each observation (including this final one here). Without such
                # a call and in case the structure of the observations change
                # sufficiently, the following `to_numpy()` call on the episode will
                # fail.
                if self.module is not None:
                    self._env_to_module(
                        episodes=[self._episode],
                        explore=explore,
                        rl_module=self.module,
                        shared_data=self._shared_data,
                        metrics=self.metrics,
                    )

                # Make the `on_episode_end` callback (before finalizing the episode,
                # but after(!) the last env-to-module connector call has been made.
                # -> All obs (even the terminal one) should have been processed now (by
                # the connector, if applicable).
                self._make_on_episode_callback("on_episode_end")

                self._prune_zero_len_sa_episodes(self._episode)

                # Numpy'ize the episode.
                if self.config.episodes_to_numpy:
                    done_episodes_to_return.append(self._episode.to_numpy())
                # Leave episode as lists of individual (obs, action, etc..) items.
                else:
                    done_episodes_to_return.append(self._episode)

                # Create a new episode instance.
                self._episode = self._new_episode()
                self._make_on_episode_callback("on_episode_created")

                # Reset the environment.
                obs, infos = self._try_env_reset()
                # Add initial observations and infos.
                self._episode.add_env_reset(observations=obs, infos=infos)

                # Make the `on_episode_start` callback.
                self._make_on_episode_callback("on_episode_start")

        # Already perform env-to-module connector call for next call to
        # `_sample_timesteps()`. See comment in c'tor for `self._cached_to_module`.
        if self.module is not None:
            self._cached_to_module = self._env_to_module(
                rl_module=self.module,
                episodes=[self._episode],
                explore=explore,
                shared_data=self._shared_data,
                metrics=self.metrics,
            )

        # Store done episodes for metrics.
        self._done_episodes_for_metrics.extend(done_episodes_to_return)

        # Also, make sure we start new episode chunks (continuing the ongoing episodes
        # from the to-be-returned chunks).
        ongoing_episode_continuation = self._episode.cut(
            len_lookback_buffer=self.config.episode_lookback_horizon
        )

        ongoing_episodes_to_return = []
        # Just started Episodes do not have to be returned. There is no data
        # in them anyway.
        if self._episode.env_t > 0:
            self._episode.validate()
            self._ongoing_episodes_for_metrics[self._episode.id_].append(self._episode)

            self._prune_zero_len_sa_episodes(self._episode)

            # Numpy'ize the episode.
            if self.config.episodes_to_numpy:
                ongoing_episodes_to_return.append(self._episode.to_numpy())
            # Leave episode as lists of individual (obs, action, etc..) items.
            else:
                ongoing_episodes_to_return.append(self._episode)

        # Continue collecting into the cut Episode chunk.
        self._episode = ongoing_episode_continuation

        # Return collected episode data.
        return done_episodes_to_return + ongoing_episodes_to_return

    def _sample_episodes(
        self,
        num_episodes: int,
        explore: bool,
        random_actions: bool = False,
    ) -> List[MultiAgentEpisode]:
        """Helper method to run n episodes.

        See docstring of `self.sample()` for more details.
        """
        # If user calls sample(num_timesteps=..) after this, we must reset again
        # at the beginning.
        self._needs_initial_reset = True

        done_episodes_to_return: List[MultiAgentEpisode] = []

        # Create a new multi-agent episode.
        _episode = self._new_episode()
        self._make_on_episode_callback("on_episode_created", _episode)
        _shared_data = {
            "agent_to_module_mapping_fn": self.config.policy_mapping_fn,
        }

        # Try resetting the environment.
        # TODO (simon): Check, if we need here the seed from the config.
        obs, infos = self._try_env_reset()
        # Set initial obs and infos in the episodes.
        _episode.add_env_reset(observations=obs, infos=infos)
        self._make_on_episode_callback("on_episode_start", _episode)

        # Loop over episodes.
        eps = 0
        ts = 0
        while eps < num_episodes:
            # Act randomly.
            if random_actions:
                # Only act (randomly) for those agents that had an observation.
                to_env = {
                    Columns.ACTIONS: [
                        {
                            aid: self.env.unwrapped.get_action_space(aid).sample()
                            for aid in self._episode.get_agents_to_act()
                        }
                    ]
                }
            # Compute an action using the RLModule.
            else:
                # Env-to-module connector.
                to_module = self._env_to_module(
                    rl_module=self.module,
                    episodes=[_episode],
                    explore=explore,
                    shared_data=_shared_data,
                    metrics=self.metrics,
                )

                # MultiRLModule forward pass: Explore or not.
                if explore:
                    env_steps_lifetime = (
                        self.metrics.peek(NUM_ENV_STEPS_SAMPLED_LIFETIME, default=0)
                        + self.metrics.peek(NUM_ENV_STEPS_SAMPLED, default=0)
                    ) * (self.config.num_env_runners or 1)
                    to_env = self.module.forward_exploration(
                        to_module, t=env_steps_lifetime
                    )
                else:
                    to_env = self.module.forward_inference(to_module)

                # Module-to-env connector.
                to_env = self._module_to_env(
                    rl_module=self.module,
                    batch=to_env,
                    episodes=[_episode],
                    explore=explore,
                    shared_data=_shared_data,
                    metrics=self.metrics,
                )

            # Extract the (vectorized) actions (to be sent to the env) from the
            # module/connector output. Note that these actions are fully ready (e.g.
            # already unsquashed/clipped) to be sent to the environment) and might not
            # be identical to the actions produced by the RLModule/distribution, which
            # are the ones stored permanently in the episode objects.
            actions = to_env.pop(Columns.ACTIONS)
            actions_for_env = to_env.pop(Columns.ACTIONS_FOR_ENV, actions)

            # Try stepping the environment.
            # TODO (sven): [0] = actions is vectorized, but env is NOT a vector Env.
            #  Support vectorized multi-agent envs.
            results = self._try_env_step(actions_for_env[0])
            # If any failure occurs during stepping -> Throw away all data collected
            # thus far and restart sampling procedure.
            if results == ENV_STEP_FAILURE:
                return self._sample_episodes(
                    num_episodes=num_episodes,
                    explore=explore,
                    random_actions=random_actions,
                )
            obs, rewards, terminateds, truncateds, infos = results

            # TODO (sven): This simple approach to re-map `to_env` from a
            #  dict[col, List[MADict]] to a dict[agentID, MADict] would not work for
            #  a vectorized env.
            extra_model_outputs = defaultdict(dict)
            for col, ma_dict_list in to_env.items():
                # TODO (sven): Support vectorized MA env.
                ma_dict = ma_dict_list[0]
                for agent_id, val in ma_dict.items():
                    extra_model_outputs[agent_id][col] = val
                    extra_model_outputs[agent_id][WEIGHTS_SEQ_NO] = self._weights_seq_no
            extra_model_outputs = dict(extra_model_outputs)

            # Record the timestep in the episode instance.
            _episode.add_env_step(
                obs,
                actions[0],
                rewards,
                infos=infos,
                terminateds=terminateds,
                truncateds=truncateds,
                extra_model_outputs=extra_model_outputs,
            )

            ts += self._increase_sampled_metrics(self.num_envs, obs, _episode)

            # Make `on_episode_step` callback before finalizing the episode.
            self._make_on_episode_callback("on_episode_step", _episode)

            # TODO (sven, simon): We have to check, if we need this elaborate
            # function here or if the `MultiAgentEnv` defines the cases that
            # can happen.
            # Right now we have:
            #   1. Most times only agents that step get `terminated`, `truncated`
            #       i.e. the rest we have to check in the episode.
            #   2. There are edge cases like, some agents terminated, all others
            #       truncated and vice versa.
            # See also `MultiAgentEpisode` for handling the `__all__`.
            if _episode.is_done:
                # Increase episode count.
                eps += 1

                # We have to perform an extra env-to-module pass here, just in case
                # the user's connector pipeline performs (permanent) transforms
                # on each observation (including this final one here). Without such
                # a call and in case the structure of the observations change
                # sufficiently, the following `to_numpy()` call on the episode will
                # fail.
                if self.module is not None:
                    self._env_to_module(
                        episodes=[_episode],
                        explore=explore,
                        rl_module=self.module,
                        shared_data=_shared_data,
                        metrics=self.metrics,
                    )

                # Make the `on_episode_end` callback (before finalizing the episode,
                # but after(!) the last env-to-module connector call has been made.
                # -> All obs (even the terminal one) should have been processed now (by
                # the connector, if applicable).
                self._make_on_episode_callback("on_episode_end", _episode)

                self._prune_zero_len_sa_episodes(_episode)

                # Numpy'ize the episode.
                if self.config.episodes_to_numpy:
                    done_episodes_to_return.append(_episode.to_numpy())
                # Leave episode as lists of individual (obs, action, etc..) items.
                else:
                    done_episodes_to_return.append(_episode)

                # Also early-out if we reach the number of episodes within this
                # for-loop.
                if eps == num_episodes:
                    break

                # Create a new episode instance.
                _episode = self._new_episode()
                self._make_on_episode_callback("on_episode_created", _episode)

                # Try resetting the environment.
                obs, infos = self._try_env_reset()
                # Add initial observations and infos.
                _episode.add_env_reset(observations=obs, infos=infos)

                # Make `on_episode_start` callback.
                self._make_on_episode_callback("on_episode_start", _episode)

        self._done_episodes_for_metrics.extend(done_episodes_to_return)

        return done_episodes_to_return

    @override(EnvRunner)
    def get_spaces(self):
        # Return the already agent-to-module translated spaces from our connector
        # pipeline.
        return {
            **{
                mid: (o, self._env_to_module.action_space[mid])
                for mid, o in self._env_to_module.observation_space.spaces.items()
            },
        }

    @override(EnvRunner)
    def get_metrics(self) -> ResultDict:
        # Compute per-episode metrics (only on already completed episodes).
        for eps in self._done_episodes_for_metrics:
            assert eps.is_done
            episode_length = len(eps)
            agent_steps = defaultdict(
                int,
                {str(aid): len(sa_eps) for aid, sa_eps in eps.agent_episodes.items()},
            )
            episode_return = eps.get_return()
            episode_duration_s = eps.get_duration_s()

            agent_episode_returns = defaultdict(
                float,
                {
                    str(sa_eps.agent_id): sa_eps.get_return()
                    for sa_eps in eps.agent_episodes.values()
                },
            )
            module_episode_returns = defaultdict(
                float,
                {
                    sa_eps.module_id: sa_eps.get_return()
                    for sa_eps in eps.agent_episodes.values()
                },
            )

            # Don't forget about the already returned chunks of this episode.
            if eps.id_ in self._ongoing_episodes_for_metrics:
                for eps2 in self._ongoing_episodes_for_metrics[eps.id_]:
                    return_eps2 = eps2.get_return()
                    episode_length += len(eps2)
                    episode_return += return_eps2
                    episode_duration_s += eps2.get_duration_s()

                    for sa_eps in eps2.agent_episodes.values():
                        return_sa = sa_eps.get_return()
                        agent_steps[str(sa_eps.agent_id)] += len(sa_eps)
                        agent_episode_returns[str(sa_eps.agent_id)] += return_sa
                        module_episode_returns[sa_eps.module_id] += return_sa

                del self._ongoing_episodes_for_metrics[eps.id_]

            self._log_episode_metrics(
                episode_length,
                episode_return,
                episode_duration_s,
                agent_episode_returns,
                module_episode_returns,
                dict(agent_steps),
            )

        # Now that we have logged everything, clear cache of done episodes.
        self._done_episodes_for_metrics.clear()

        # Return reduced metrics.
        return self.metrics.reduce()

    @override(Checkpointable)
    def get_state(
        self,
        components: Optional[Union[str, Collection[str]]] = None,
        *,
        not_components: Optional[Union[str, Collection[str]]] = None,
        **kwargs,
    ) -> StateDict:
        # Basic state dict.
        state = {
            NUM_ENV_STEPS_SAMPLED_LIFETIME: (
                self.metrics.peek(NUM_ENV_STEPS_SAMPLED_LIFETIME, default=0)
            ),
        }

        # RLModule (MultiRLModule) component.
        if self._check_component(COMPONENT_RL_MODULE, components, not_components):
            state[COMPONENT_RL_MODULE] = self.module.get_state(
                components=self._get_subcomponents(COMPONENT_RL_MODULE, components),
                not_components=self._get_subcomponents(
                    COMPONENT_RL_MODULE, not_components
                ),
                **kwargs,
            )
            state[WEIGHTS_SEQ_NO] = self._weights_seq_no

        # Env-to-module connector.
        if self._check_component(
            COMPONENT_ENV_TO_MODULE_CONNECTOR, components, not_components
        ):
            state[COMPONENT_ENV_TO_MODULE_CONNECTOR] = self._env_to_module.get_state()
        # Module-to-env connector.
        if self._check_component(
            COMPONENT_MODULE_TO_ENV_CONNECTOR, components, not_components
        ):
            state[COMPONENT_MODULE_TO_ENV_CONNECTOR] = self._module_to_env.get_state()

        return state

    @override(Checkpointable)
    def set_state(self, state: StateDict) -> None:
        if COMPONENT_ENV_TO_MODULE_CONNECTOR in state:
            self._env_to_module.set_state(state[COMPONENT_ENV_TO_MODULE_CONNECTOR])
        if COMPONENT_MODULE_TO_ENV_CONNECTOR in state:
            self._module_to_env.set_state(state[COMPONENT_MODULE_TO_ENV_CONNECTOR])

        # Update RLModule state.
        if COMPONENT_RL_MODULE in state:
            # A missing value for WEIGHTS_SEQ_NO or a value of 0 means: Force the
            # update.
            weights_seq_no = state.get(WEIGHTS_SEQ_NO, 0)

            # Only update the weigths, if this is the first synchronization or
            # if the weights of this `EnvRunner` lacks behind the actual ones.
            if weights_seq_no == 0 or self._weights_seq_no < weights_seq_no:
                rl_module_state = state[COMPONENT_RL_MODULE]
                if isinstance(rl_module_state, ray.ObjectRef):
                    rl_module_state = ray.get(rl_module_state)
                self.module.set_state(rl_module_state)

            # Update weights_seq_no, if the new one is > 0.
            if weights_seq_no > 0:
                self._weights_seq_no = weights_seq_no

        # Update lifetime counters.
        if NUM_ENV_STEPS_SAMPLED_LIFETIME in state:
            self.metrics.set_value(
                key=NUM_ENV_STEPS_SAMPLED_LIFETIME,
                value=state[NUM_ENV_STEPS_SAMPLED_LIFETIME],
                reduce="sum",
                with_throughput=True,
            )

    @override(Checkpointable)
    def get_ctor_args_and_kwargs(self):
        return (
            (),  # *args
            {"config": self.config},  # **kwargs
        )

    @override(Checkpointable)
    def get_metadata(self):
        metadata = Checkpointable.get_metadata(self)
        metadata.update(
            {
                # TODO (sven): Maybe add serialized (JSON-writable) config here?
            }
        )
        return metadata

    @override(Checkpointable)
    def get_checkpointable_components(self):
        return [
            (COMPONENT_RL_MODULE, self.module),
            (COMPONENT_ENV_TO_MODULE_CONNECTOR, self._env_to_module),
            (COMPONENT_MODULE_TO_ENV_CONNECTOR, self._module_to_env),
        ]

    @override(EnvRunner)
    def assert_healthy(self):
        """Checks that self.__init__() has been completed properly.

        Ensures that the instances has a `MultiRLModule` and an
        environment defined.

        Raises:
            AssertionError: If the EnvRunner Actor has NOT been properly initialized.
        """
        # Make sure, we have built our gym.vector.Env and RLModule properly.
        assert self.env and self.module

    @override(EnvRunner)
    def make_env(self):
        # If an env already exists, try closing it first (to allow it to properly
        # cleanup).
        if self.env is not None:
            try:
                self.env.close()
            except Exception as e:
                logger.warning(
                    "Tried closing the existing env (multi-agent), but failed with "
                    f"error: {e.args[0]}"
                )
            del self.env

        env_ctx = self.config.env_config
        if not isinstance(env_ctx, EnvContext):
            env_ctx = EnvContext(
                env_ctx,
                worker_index=self.worker_index,
                num_workers=self.config.num_env_runners,
                remote=self.config.remote_worker_envs,
            )

        # No env provided -> Error.
        if not self.config.env:
            raise ValueError(
                "`config.env` is not provided! You should provide a valid environment "
                "to your config through `config.environment([env descriptor e.g. "
                "'CartPole-v1'])`."
            )
        # Register env for the local context.
        # Note, `gym.register` has to be called on each worker.
        elif isinstance(self.config.env, str) and _global_registry.contains(
            ENV_CREATOR, self.config.env
        ):
            entry_point = partial(
                _global_registry.get(ENV_CREATOR, self.config.env),
                env_ctx,
            )
        else:
            entry_point = partial(
                _gym_env_creator,
                env_descriptor=self.config.env,
                env_context=env_ctx,
            )
        gym.register(
            "rllib-multi-agent-env-v0",
            entry_point=entry_point,
            disable_env_checker=True,
        )

        # Perform actual gym.make call.
        self.env: MultiAgentEnv = gym.make("rllib-multi-agent-env-v0")
        self.num_envs = 1
        # If required, check the created MultiAgentEnv.
        if not self.config.disable_env_checking:
            try:
                check_multiagent_environments(self.env.unwrapped)
            except Exception as e:
                logger.exception(e.args[0])
        # If not required, still check the type (must be MultiAgentEnv).
        else:
            assert isinstance(self.env.unwrapped, MultiAgentEnv), (
                "ERROR: When using the `MultiAgentEnvRunner` the environment needs "
                "to inherit from `ray.rllib.env.multi_agent_env.MultiAgentEnv`."
            )

        # Set the flag to reset all envs upon the next `sample()` call.
        self._needs_initial_reset = True

        # Call the `on_environment_created` callback.
        make_callback(
            "on_environment_created",
            callbacks_objects=self._callbacks,
            callbacks_functions=self.config.callbacks_on_environment_created,
            kwargs=dict(
                env_runner=self,
                metrics_logger=self.metrics,
                env=self.env.unwrapped,
                env_context=env_ctx,
            ),
        )

    @override(EnvRunner)
    def make_module(self):
        try:
            module_spec: MultiRLModuleSpec = self.config.get_multi_rl_module_spec(
                env=self.env.unwrapped, spaces=self.get_spaces(), inference_only=True
            )
            # Build the module from its spec.
            self.module = module_spec.build()
            # Move the RLModule to our device.
            # TODO (sven): In order to make this framework-agnostic, we should maybe
            #  make the MultiRLModule.build() method accept a device OR create an
            #  additional `(Multi)RLModule.to()` override.
            if torch:
                self.module.foreach_module(
                    lambda mid, mod: (
                        mod.to(self._device)
                        if isinstance(mod, torch.nn.Module)
                        else mod
                    )
                )

        # If `AlgorithmConfig.get_rl_module_spec()` is not implemented, this env runner
        # will not have an RLModule, but might still be usable with random actions.
        except NotImplementedError:
            self.module = None

    @override(EnvRunner)
    def stop(self):
        # Note, `MultiAgentEnv` inherits `close()`-method from `gym.Env`.
        self.env.close()

    def _setup_metrics(self):
        self.metrics = MetricsLogger()

        self._done_episodes_for_metrics: List[MultiAgentEpisode] = []
        self._ongoing_episodes_for_metrics: DefaultDict[
            EpisodeID, List[MultiAgentEpisode]
        ] = defaultdict(list)

    def _new_episode(self):
        return MultiAgentEpisode(
            observation_space={
                aid: self.env.unwrapped.get_observation_space(aid)
                for aid in self.env.unwrapped.possible_agents
            },
            action_space={
                aid: self.env.unwrapped.get_action_space(aid)
                for aid in self.env.unwrapped.possible_agents
            },
            agent_to_module_mapping_fn=self.config.policy_mapping_fn,
        )

    def _make_on_episode_callback(self, which: str, episode=None):
        episode = episode if episode is not None else self._episode
        make_callback(
            which,
            callbacks_objects=self._callbacks,
            callbacks_functions=getattr(self.config, f"callbacks_{which}"),
            kwargs=dict(
                episode=episode,
                env_runner=self,
                metrics_logger=self.metrics,
                env=self.env.unwrapped,
                rl_module=self.module,
                env_index=0,
            ),
        )

    def _increase_sampled_metrics(self, num_steps, next_obs, episode):
        # Env steps.
        self.metrics.log_value(
            NUM_ENV_STEPS_SAMPLED, num_steps, reduce="sum", clear_on_reduce=True
        )
        self.metrics.log_value(
            NUM_ENV_STEPS_SAMPLED_LIFETIME,
            num_steps,
            reduce="sum",
            with_throughput=True,
        )
        # Completed episodes.
        if episode.is_done:
            self.metrics.log_value(NUM_EPISODES, 1, reduce="sum", clear_on_reduce=True)
            self.metrics.log_value(NUM_EPISODES_LIFETIME, 1, reduce="sum")

        # TODO (sven): obs is not-vectorized. Support vectorized MA envs.
        for aid in next_obs:
            self.metrics.log_value(
                (NUM_AGENT_STEPS_SAMPLED, str(aid)),
                1,
                reduce="sum",
                clear_on_reduce=True,
            )
            self.metrics.log_value(
                (NUM_AGENT_STEPS_SAMPLED_LIFETIME, str(aid)),
                1,
                reduce="sum",
            )
            self.metrics.log_value(
                (NUM_MODULE_STEPS_SAMPLED, episode.module_for(aid)),
                1,
                reduce="sum",
                clear_on_reduce=True,
            )
            self.metrics.log_value(
                (NUM_MODULE_STEPS_SAMPLED_LIFETIME, episode.module_for(aid)),
                1,
                reduce="sum",
            )
        return num_steps

    def _log_episode_metrics(
        self,
        length,
        ret,
        sec,
        agents=None,
        modules=None,
        agent_steps=None,
    ):
        # Log general episode metrics.

        # Use the configured window, but factor in the parallelism of the EnvRunners.
        # As a result, we only log the last `window / num_env_runners` steps here,
        # b/c everything gets parallel-merged in the Algorithm process.
        win = max(
            1,
            int(
                math.ceil(
                    self.config.metrics_num_episodes_for_smoothing
                    / (self.config.num_env_runners or 1)
                )
            ),
        )

        self.metrics.log_dict(
            {
                EPISODE_LEN_MEAN: length,
                EPISODE_RETURN_MEAN: ret,
                EPISODE_DURATION_SEC_MEAN: sec,
                **(
                    {
                        # Per-agent returns.
                        "agent_episode_returns_mean": agents,
                        # Per-RLModule returns.
                        "module_episode_returns_mean": modules,
                        "agent_steps": agent_steps,
                    }
                    if agents is not None
                    else {}
                ),
            },
            window=win,
        )
        # For some metrics, log min/max as well.
        self.metrics.log_dict(
            {
                EPISODE_LEN_MIN: length,
                EPISODE_RETURN_MIN: ret,
            },
            reduce="min",
            window=win,
        )
        self.metrics.log_dict(
            {
                EPISODE_LEN_MAX: length,
                EPISODE_RETURN_MAX: ret,
            },
            reduce="max",
            window=win,
        )

    @staticmethod
    def _prune_zero_len_sa_episodes(episode: MultiAgentEpisode):
        for agent_id, agent_eps in episode.agent_episodes.copy().items():
            if len(agent_eps) == 0:
                del episode.agent_episodes[agent_id]

    @Deprecated(
        new="MultiAgentEnvRunner.get_state(components='rl_module')",
        error=False,
    )
    def get_weights(self, modules=None):
        rl_module_state = self.get_state(components=COMPONENT_RL_MODULE)[
            COMPONENT_RL_MODULE
        ]
        return rl_module_state

    @Deprecated(new="MultiAgentEnvRunner.set_state()", error=False)
    def set_weights(
        self,
        weights: ModelWeights,
        global_vars: Optional[Dict] = None,
        weights_seq_no: int = 0,
    ) -> None:
        assert global_vars is None
        return self.set_state(
            {
                COMPONENT_RL_MODULE: weights,
                WEIGHTS_SEQ_NO: weights_seq_no,
            }
        )
