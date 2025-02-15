# Copyright 2022 Sony Group Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import gym
import numpy as np
import pickle

import nnabla as nn
import nnabla.functions as F
import nnabla.parametric_functions as PF
import nnabla.initializer as I
import nnabla.solvers as S
import nnabla_rl.hooks as H
import nnabla_rl.initializers as RI
from nnabla_rl.distributions import Gaussian, Distribution
from nnabla_rl.models.reward_function import RewardFunction
from nnabla_rl.algorithms import GAIL, GAILConfig
from nnabla_rl.builders import ModelBuilder, SolverBuilder
from nnabla_rl.environments.wrappers import ScreenRenderEnv, NumpyFloat32Env
from nnabla_rl.models import StochasticPolicy, VFunction
from nnabla_rl.utils.reproductions import build_mujoco_env, d4rl_dataset_to_experiences  # noqa
from nnabla_rl.replay_buffers import ReplacementSamplingReplayBuffer  # noqa
from nnabla_rl.utils.evaluator import EpisodicEvaluator
from nnabla_rl.writers import FileWriter


def build_classic_control_env(env_name, render=False):
    env = gym.make(env_name)
    env = NumpyFloat32Env(env)
    if render:
        # render environment if render is True
        env = ScreenRenderEnv(env)
    return env


class ExampleClassicControlVFunction(VFunction):
    def __init__(self, scope_name: str):
        super(ExampleClassicControlVFunction, self).__init__(scope_name)

    def v(self, s: nn.Variable) -> nn.Variable:
        with nn.parameter_scope(self.scope_name):
            with nn.parameter_scope("affine1"):
                h = PF.affine(s, n_outmaps=64)
                h = F.relu(h)
            with nn.parameter_scope("affine2"):
                h = PF.affine(h, n_outmaps=64)
                h = F.relu(h)
            with nn.parameter_scope("affine3"):
                h = PF.affine(h, n_outmaps=1)
        return h


class ExampleMujocoVFunction(VFunction):
    def __init__(self, scope_name: str):
        super(ExampleMujocoVFunction, self).__init__(scope_name)

    def v(self, s: nn.Variable) -> nn.Variable:
        with nn.parameter_scope(self.scope_name):
            h = PF.affine(s, n_outmaps=100, name="linear1",
                          w_init=RI.NormcInitializer(std=1.0))
            h = F.tanh(x=h)
            h = PF.affine(h, n_outmaps=100, name="linear2",
                          w_init=RI.NormcInitializer(std=1.0))
            h = F.tanh(x=h)
            h = PF.affine(h, n_outmaps=1, name="linear3",
                          w_init=RI.NormcInitializer(std=1.0))
        return h


class ExampleClassicControlPolicy(StochasticPolicy):
    def __init__(self, scope_name: str, action_dim: int):
        super(ExampleClassicControlPolicy, self).__init__(scope_name)
        self._action_dim = action_dim

    def pi(self, s: nn.Variable) -> Distribution:
        with nn.parameter_scope(self.scope_name):
            with nn.parameter_scope("affine1"):
                h = PF.affine(s, n_outmaps=64,
                              w_init=I.OrthogonalInitializer(np.sqrt(2.0)))
                h = F.relu(h)
            with nn.parameter_scope("affine2"):
                h = PF.affine(h, n_outmaps=64,
                              w_init=I.OrthogonalInitializer(np.sqrt(2.0)))
                h = F.relu(h)
            with nn.parameter_scope("affine3"):
                h = PF.affine(h, n_outmaps=self._action_dim * 2,
                              w_init=I.OrthogonalInitializer(np.sqrt(0.01)))
            reshaped = F.reshape(h, shape=(-1, 2, self._action_dim))
            mean, ln_sigma = F.split(reshaped, axis=1)
            ln_var = ln_sigma * 2.0
        return Gaussian(mean=mean, ln_var=ln_var)


class ExampleMujocoPolicy(StochasticPolicy):
    def __init__(self, scope_name: str, action_dim: str):
        super(ExampleMujocoPolicy, self).__init__(scope_name)
        self._action_dim = action_dim

    def pi(self, s: nn.Variable) -> Distribution:
        with nn.parameter_scope(self.scope_name):
            h = PF.affine(s, n_outmaps=100, name="linear1",
                          w_init=RI.NormcInitializer(std=1.0))
            h = F.tanh(x=h)
            h = PF.affine(h, n_outmaps=100, name="linear2",
                          w_init=RI.NormcInitializer(std=1.0))
            h = F.tanh(x=h)
            mean = PF.affine(h, n_outmaps=self._action_dim,
                             name="linear3", w_init=RI.NormcInitializer(std=0.01))
            assert mean.shape == (s.shape[0], self._action_dim)
            ln_sigma = nn.parameter.get_parameter_or_create(
                "ln_sigma", shape=(1, self._action_dim), initializer=I.ConstantInitializer(0.0)
            )
            ln_var = F.broadcast(
                ln_sigma, (s.shape[0], self._action_dim)) * 2.0
        return Gaussian(mean, ln_var)


class ExampleClassicDiscriminator(RewardFunction):
    def __init__(self, scope_name: str):
        super(ExampleClassicDiscriminator, self).__init__(scope_name)

    def r(self, s_current: nn.Variable, a_current: nn.Variable, s_next: nn.Variable) -> nn.Variable:
        """
        Notes:
            In gail, we don't use the next state.
        """
        h = F.concatenate(s_current, a_current, axis=1)
        with nn.parameter_scope(self.scope_name):
            h = PF.affine(h, n_outmaps=64, name="linear1",
                          w_init=RI.GlorotUniform(h.shape[1], 64))
            h = F.tanh(x=h)
            h = PF.affine(h, n_outmaps=64, name="linear2",
                          w_init=RI.GlorotUniform(h.shape[1], 64))
            h = F.tanh(x=h)
            h = PF.affine(h, n_outmaps=1, name="linear3",
                          w_init=RI.GlorotUniform(h.shape[1], 1))

        return h


class ExampleMujocoDiscriminator(RewardFunction):
    def __init__(self, scope_name: str):
        super(ExampleMujocoDiscriminator, self).__init__(scope_name)

    def r(self, s_current: nn.Variable, a_current: nn.Variable, s_next: nn.Variable) -> nn.Variable:
        """
        Notes:
            In gail, we don't use the next state.
        """
        h = F.concatenate(s_current, a_current, axis=1)
        with nn.parameter_scope(self.scope_name):
            h = PF.affine(h, n_outmaps=100, name="linear1",
                          w_init=RI.GlorotUniform(h.shape[1], 100))
            h = F.tanh(x=h)
            h = PF.affine(h, n_outmaps=100, name="linear2",
                          w_init=RI.GlorotUniform(h.shape[1], 100))
            h = F.tanh(x=h)
            h = PF.affine(h, n_outmaps=1, name="linear3",
                          w_init=RI.GlorotUniform(h.shape[1], 1))

        return h


class ExamplePolicyBuilder(ModelBuilder):
    def __init__(self, is_mujoco=False):
        self._is_mujoco = is_mujoco

    def build_model(self, scope_name, env_info, algorithm_config, **kwargs):
        if self._is_mujoco:
            return ExampleMujocoPolicy(scope_name, env_info.action_dim)
        else:
            return ExampleClassicControlPolicy(scope_name, env_info.action_dim)


class ExampleVFunctionBuilder(ModelBuilder):
    def __init__(self, is_mujoco=False):
        self._is_mujoco = is_mujoco

    def build_model(self, scope_name, env_info, algorithm_config, **kwargs):
        if self._is_mujoco:
            return ExampleMujocoVFunction(scope_name)
        else:
            return ExampleClassicControlVFunction(scope_name)


class ExampleVSolverBuilder(SolverBuilder):
    def build_solver(self, env_info, algorithm_config, **kwargs):
        config: GAILConfig = algorithm_config
        solver = S.Adam(alpha=config.vf_learning_rate)
        return solver


class ExampleRewardFunctionBuilder(ModelBuilder):
    def __init__(self, is_mujoco=False):
        self._is_mujoco = is_mujoco

    def build_model(self, scope_name, env_info, algorithm_config, **kwargs):
        if self._is_mujoco:
            return ExampleMujocoDiscriminator(scope_name)
        else:
            return ExampleClassicDiscriminator(scope_name)


class ExampleRewardFunctionSolverBuilder(SolverBuilder):
    def build_solver(self, env_info, algorithm_config, **kwargs):
        config: GAILConfig = algorithm_config
        solver = S.Adam(alpha=config.discriminator_learning_rate)
        return solver


def train():
    # nnabla-rl's Reinforcement learning algorithm requires environment that implements gym.Env interface
    # for the details of gym.Env see: https://github.com/openai/gym
    env_name = "Pendulum-v1"
    train_env = build_classic_control_env(env_name)
    # load expert dataset
    with open("./pendulum_v1_expert_buffer.pkl", mode="rb") as f:
        expert_buffer = pickle.load(f)

    # evaluation env is used only for running the evaluation of models during the training.
    # if you do not evaluate the model during the training, this environment is not necessary.
    eval_env = build_classic_control_env(env_name, render=True)
    evaluation_timing = 10000
    total_iterations = 1000000
    pi_batch_size = 10000
    discriminator_batch_size = 10000
    num_steps_per_iteration = 10000
    is_mujoco = False

    # If you want to train on mujoco, uncomment below
    # You can change the name of environment to change the environment to train.
    # You also need to install d4rl. See: https://github.com/rail-berkeley/d4rl
    # env_name = "halfcheetah-medium-v2"
    # train_env = build_mujoco_env(env_name)
    # eval_env = build_mujoco_env(env_name, test=True, render=True)
    # get expert dataset
    # train_dataset = train_env.get_dataset()
    # expert_buffer = ReplacementSamplingReplayBuffer(capacity=4000)
    # expert_experiences = d4rl_dataset_to_experiences(train_dataset, size=4000)
    # expert_buffer.append_all(expert_experiences)
    # evaluation_timing = 50000
    # total_iterations = 25000000
    # pi_batch_size = 50000
    # discriminator_batch_size = 50000
    # num_steps_per_iteration = 50000
    # is_mujoco = True

    # Will output evaluation results and model snapshots to the outdir
    outdir = f"{env_name}_results"

    # Writer will save the evaluation results to file.
    # If you set writer=None, evaluator will only print the evaluation results on terminal.
    writer = FileWriter(outdir, "evaluation_result")
    evaluator = EpisodicEvaluator(run_per_evaluation=5)
    # evaluate the trained model with eval_env every 5000 iterations
    evaluation_hook = H.EvaluationHook(
        eval_env, evaluator, timing=evaluation_timing, writer=writer)

    # This will print the iteration number every 100 iteration.
    # Printing iteration number is convenient for checking the training progress.
    # You can change this number to any number of your choice.
    iteration_num_hook = H.IterationNumHook(timing=100)

    # save the trained model every 5000 iterations
    save_snapshot_hook = H.SaveSnapshotHook(outdir, timing=evaluation_timing)

    # Set gpu_id to -1 to train on cpu.
    gpu_id = 0
    config = GAILConfig(
        gpu_id=gpu_id,
        pi_batch_size=pi_batch_size,
        num_steps_per_iteration=num_steps_per_iteration,
        discriminator_batch_size=discriminator_batch_size,
    )
    gail = GAIL(
        train_env,
        expert_buffer,
        config=config,
        policy_builder=ExamplePolicyBuilder(is_mujoco=is_mujoco),
        v_function_builder=ExampleVFunctionBuilder(is_mujoco=is_mujoco),
        v_solver_builder=ExampleVSolverBuilder(),
        reward_function_builder=ExampleRewardFunctionBuilder(
            is_mujoco=is_mujoco),
        reward_solver_builder=ExampleRewardFunctionSolverBuilder(),
    )
    # Set instanciated hooks to periodically run additional jobs
    gail.set_hooks(
        hooks=[evaluation_hook, iteration_num_hook, save_snapshot_hook])
    gail.train(train_env, total_iterations=total_iterations)


if __name__ == "__main__":
    train()
