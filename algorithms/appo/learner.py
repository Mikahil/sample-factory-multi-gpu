import copy
import threading
import time
from collections import OrderedDict, deque
from queue import Empty, Queue
from threading import Thread

import numpy as np
import ray.pyarrow_files.pyarrow as pa
import torch
from ray.pyarrow_files.pyarrow import plasma
from torch.multiprocessing import JoinableQueue, Process

from algorithms.appo.appo_utils import TaskType, dict_of_lists_append, list_of_dicts_to_dict_of_lists, \
    iterate_recursively
from algorithms.appo.model import ActorCritic
from algorithms.utils.action_distributions import get_action_distribution
from algorithms.utils.algo_utils import calculate_gae
from algorithms.utils.multi_env import safe_get
from utils.decay import LinearDecay
from utils.timing import Timing
from utils.utils import log, AttrDict


class LearnerWorker:
    def __init__(
        self, worker_idx, policy_id, cfg, obs_space, action_space, plasma_store_name, report_queue, weight_queues,
    ):
        log.info('Initializing GPU learner %d for policy %d', worker_idx, policy_id)

        self.worker_idx = worker_idx
        self.policy_id = policy_id
        self.cfg = cfg
        self.policy_version = 0

        self.with_training = True  # TODO: debug, remove
        self.terminate = False

        self.obs_space = obs_space
        self.action_space = action_space

        self.plasma_store_name = plasma_store_name

        # initialize the Torch modules
        self.device = None
        self.actor_critic = None
        self.optimizer = None

        self.task_queue = JoinableQueue()
        self.report_queue = report_queue
        self.weight_queues = weight_queues

        self.experience_buffer_queue = Queue()

        self.train_in_background = True
        self.training_thread = Thread(target=self._train_loop) if self.train_in_background else None
        self.train_thread_initialized = threading.Event()

        self.train_step = 0
        self.summary_rate_decay = LinearDecay([(0, 100), (1000000, 2000), (10000000, 10000)])
        self.last_summary_written = -1e9

        # some stats we measure in the end of the last training epoch
        self.last_batch_stats = AttrDict()

        self.kl_coeff = self.cfg.initial_kl_coeff

        self.process = Process(target=self._run, daemon=True)

    def start_process(self):
        self.process.start()

    def _init(self):
        log.info('Waiting for GPU learner to initialize...')
        self.train_thread_initialized.wait()
        log.info('GPU learner %d initialized', self.worker_idx)

    def _terminate(self):
        self.terminate = True

    def _broadcast_weights(self):
        state_dict = self.actor_critic.state_dict()
        weight_update = (self.policy_version, state_dict)
        for q in self.weight_queues:
            q.put((TaskType.UPDATE_WEIGHTS, weight_update))

    def _calculate_gae(self, buffer):
        rewards = np.asarray(buffer.rewards)  # [E, T]
        dones = np.asarray(buffer.dones)  # [E, T]
        values_arr = np.array(buffer.values).squeeze()  # [E, T]

        # calculating fake values for the last step in the rollout
        # this will make sure that advantage of the very last action is always zero
        values = []
        for i in range(len(values_arr)):
            last_value, last_reward = values_arr[i][-1], rewards[i, -1]
            next_value = (last_value - last_reward) / self.cfg.gamma
            values.append(list(values_arr[i]))
            values[i].append(float(next_value))  # [T] -> [T+1]

        # calculating returns and GAE
        rewards = rewards.transpose((1, 0))  # [E, T] -> [T, E]
        dones = dones.transpose((1, 0))  # [E, T] -> [T, E]
        values = np.asarray(values).transpose((1, 0))  # [E, T+1] -> [T+1, E]

        advantages, returns = calculate_gae(rewards, dones, values, self.cfg.gamma, self.cfg.gae_lambda)

        # transpose tensors back to [E, T] before creating a single experience buffer
        buffer.advantages = advantages.transpose((1, 0))  # [T, E] -> [E, T]
        buffer.returns = returns.transpose((1, 0))  # [T, E] -> [E, T]
        buffer.returns = buffer.returns[:, :, np.newaxis]  # [E, T] -> [E, T, 1]

        return buffer

    def _prepare_train_buffer(self, rollouts, timing):
        trajectories = [AttrDict(r['t']) for r in rollouts]
        log.info('%r', trajectories[0].policy_version)
        log.info('%r', trajectories[-1].policy_version)

        with timing.add_time('buffers'):
            buffer = AttrDict()

            # by the end of this loop the buffer is a dictionary containing lists of numpy arrays
            for i, t in enumerate(trajectories):
                for key, x in t.items():
                    if key not in buffer:
                        buffer[key] = []
                    buffer[key].append(x)

            # convert lists of dict observations to a single dictionary of lists
            for key, x in buffer.items():
                if isinstance(x[0], (dict, OrderedDict)):
                    buffer[key] = list_of_dicts_to_dict_of_lists(x)

        with timing.add_time('calc_gae'):
            buffer = self._calculate_gae(buffer)

        with timing.add_time('tensors'):
            for d, key, value in iterate_recursively(buffer):
                value = np.asarray(value)
                value = torch.tensor(value, device=self.device)
                value = value.float()
                d[key] = value.reshape(-1, *value.shape[2:])

            experience_size = buffer.rewards.shape[0]  # could have used any other key

        # normalize advantages if needed
        if self.cfg.normalize_advantage:
            adv_mean = buffer.advantages.mean()
            adv_std = buffer.advantages.std()
            # adv_max, adv_min = buffer.advantages.max(), buffer.advantages.min()
            # adv_max_abs = max(adv_max, abs(adv_min))
            # log.info(
            #     'Adv mean %.3f std %.3f, min %.3f, max %.3f, max abs %.3f',
            #     adv_mean, adv_std, adv_min, adv_max, adv_max_abs,
            # )
            buffer.advantages = (buffer.advantages - adv_mean) / max(1e-2, adv_std)

        return buffer, experience_size

    def _process_macro_batch(self, rollouts, timing):
        assert self.cfg.macro_batch % self.cfg.rollout == 0
        assert self.cfg.rollout % self.cfg.recurrence == 0
        assert self.cfg.macro_batch % self.cfg.recurrence == 0

        samples = env_steps = 0
        for rollout in rollouts:
            samples += rollout['length']
            env_steps += rollout['env_steps']

        with timing.add_time('prepare'):
            buffer, experience_size = self._prepare_train_buffer(rollouts, timing)
            self.experience_buffer_queue.put((buffer, experience_size, samples, env_steps))

    def _process_rollouts(self, rollouts, timing):
        # log.info('Pending rollouts: %d (%d samples)', len(self.rollouts), len(self.rollouts) * self.cfg.rollout)
        rollouts_in_macro_batch = self.cfg.macro_batch // self.cfg.rollout

        while len(rollouts) >= rollouts_in_macro_batch:
            rollouts_to_process = rollouts[:rollouts_in_macro_batch]
            rollouts = rollouts[rollouts_in_macro_batch:]

            self._process_macro_batch(rollouts_to_process, timing)
            log.info('Unprocessed rollouts: %d (%d samples)', len(rollouts), len(rollouts) * self.cfg.rollout)

        return rollouts

    def _get_minibatches(self, experience_size):
        """Generating minibatches for training."""
        assert self.cfg.rollout % self.cfg.recurrence == 0
        assert experience_size % self.cfg.batch_size == 0

        # indices that will start the mini-trajectories from the same episode (for bptt)
        indices = np.arange(0, experience_size, self.cfg.recurrence)
        indices = np.random.permutation(indices)

        # complete indices of mini trajectories, e.g. with recurrence==4: [4, 16] -> [4, 5, 6, 7, 16, 17, 18, 19]
        indices = [np.arange(i, i + self.cfg.recurrence) for i in indices]
        indices = np.concatenate(indices)

        assert len(indices) == experience_size

        num_minibatches = experience_size // self.cfg.batch_size
        minibatches = np.split(indices, num_minibatches)
        return minibatches

    @staticmethod
    def _get_minibatch(buffer, indices):
        mb = AttrDict()

        for item, x in buffer.items():
            if isinstance(x, (dict, OrderedDict)):
                mb[item] = AttrDict()
                for key, x_elem in x.items():
                    mb[item][key] = x_elem[indices]
            else:
                mb[item] = x[indices]

        return mb

    def _should_save_summaries(self):
        summaries_every = self.summary_rate_decay.at(self.train_step)
        return self.train_step - self.last_summary_written > summaries_every

    def _after_optimizer_step(self):
        """A hook to be called after each optimizer step."""
        self.train_step += 1
        self.policy_version += 1
        # self._maybe_save()
        # TODO!!

    def _policy_loss(self, action_distribution, mb, clip_ratio):
        log_prob_actions = action_distribution.log_prob(mb.actions)
        ratio = torch.exp(log_prob_actions - mb.log_prob_actions)  # pi / pi_old

        # p_old = torch.exp(mb.log_prob_actions)
        positive_clip_ratio = clip_ratio
        negative_clip_ratio = 1.0 / clip_ratio

        is_adv_positive = (mb.advantages > 0.0).float()
        is_ratio_too_big = (ratio > positive_clip_ratio).float() * is_adv_positive

        is_adv_negative = (mb.advantages < 0.0).float()
        is_ratio_too_small = (ratio < negative_clip_ratio).float() * is_adv_negative

        clipping = is_adv_positive * positive_clip_ratio + is_adv_negative * negative_clip_ratio

        is_ratio_clipped = is_ratio_too_big + is_ratio_too_small
        is_ratio_not_clipped = 1.0 - is_ratio_clipped

        # total_non_clipped = torch.sum(is_ratio_not_clipped).float()
        fraction_clipped = is_ratio_clipped.mean()

        objective = ratio * mb.advantages
        leak = 0.0  # currently not used
        objective_clipped = -leak * ratio * mb.advantages + clipping * mb.advantages * (1.0 + leak)

        policy_loss = -(objective * is_ratio_not_clipped + objective_clipped * is_ratio_clipped)
        policy_loss = policy_loss.mean()

        return policy_loss, ratio, fraction_clipped

    def _value_loss(self, new_values, mb, clip_value):
        value_clipped = mb.values + torch.clamp(new_values - mb.values, -clip_value, clip_value)
        value_original_loss = (new_values - mb.returns).pow(2)
        value_clipped_loss = (value_clipped - mb.returns).pow(2)
        value_loss = torch.max(value_original_loss, value_clipped_loss)
        value_loss = value_loss.mean()
        value_loss *= self.cfg.value_loss_coeff
        value_delta = torch.abs(new_values - mb.values)

        return value_loss, value_delta

    def _train(self, buffer, experience_size, timing):
        stats = None

        clip_ratio = self.cfg.ppo_clip_ratio
        clip_value = self.cfg.ppo_clip_value

        kl_old_mean = kl_old_max = 0.0
        value_delta_avg = value_delta_max = 0.0
        fraction_clipped = 0.0
        rnn_dist = 0.0
        ratio_mean = ratio_min = ratio_max = 0.0

        early_stopping = False
        num_sgd_steps = 0

        for epoch in range(self.cfg.ppo_epochs):
            if early_stopping:
                break

            minibatches = self._get_minibatches(experience_size)

            for batch_num in range(len(minibatches)):
                indices = minibatches[batch_num]

                # current minibatch consisting of short trajectory segments with length == recurrence
                mb = self._get_minibatch(buffer, indices)

                # calculate policy head outside of recurrent loop
                with timing.add_time('forw_head'):
                    head_outputs = self.actor_critic.forward_head(mb.obs)

                # indices corresponding to 1st frames of trajectory segments
                traj_indices = indices[::self.cfg.recurrence]

                # initial rnn states
                rnn_states = buffer.rnn_states[traj_indices]

                # calculate RNN outputs for each timestep in a loop
                core_outputs = []
                for i in range(self.cfg.recurrence):
                    # indices of head outputs corresponding to the current timestep
                    timestep_indices = np.arange(i, self.cfg.batch_size, self.cfg.recurrence)
                    step_head_outputs = head_outputs[timestep_indices]

                    dones = mb.dones[timestep_indices].unsqueeze(dim=1)
                    rnn_states = (1.0 - dones) * rnn_states + dones * mb.rnn_states[timestep_indices]

                    with timing.add_time('forw_core'):
                        core_output, rnn_states = self.actor_critic.forward_core(step_head_outputs, rnn_states)

                    core_outputs.append(core_output)

                # transform core outputs from [T, Batch, D] to [Batch, T, D] and then to [Batch x T, D]
                # which is the same shape as the minibatch
                core_outputs = torch.stack(core_outputs)
                core_outputs = core_outputs.transpose(0, 1).reshape(-1, *core_outputs.shape[2:])
                assert core_outputs.shape[0] == head_outputs.shape[0]

                # calculate policy tail outside of recurrent loop
                with timing.add_time('forw_tail'):
                    result = self.actor_critic.forward_tail(core_outputs, with_action_distribution=True)

                action_distribution = result.action_distribution

                policy_loss, ratio, fraction_clipped = self._policy_loss(action_distribution, mb, clip_ratio)
                ratio_mean = torch.abs(1.0 - ratio).mean()
                ratio_min = ratio.min()
                ratio_max = ratio.max()

                value_loss, value_delta = self._value_loss(result.values, mb, clip_value)
                value_delta_avg, value_delta_max = value_delta.mean(), value_delta.max()

                # entropy loss
                entropy = action_distribution.entropy().mean()
                kl_prior = action_distribution.kl_prior()
                kl_prior = kl_prior.mean()
                prior_loss = self.cfg.prior_loss_coeff * kl_prior

                old_action_distribution = get_action_distribution(self.actor_critic.action_space, mb.action_logits)

                # small KL penalty for being different from the behavior policy
                kl_old = action_distribution.kl_divergence(old_action_distribution)
                kl_old_max = kl_old.max()
                kl_old_mean = kl_old.mean()
                kl_penalty = self.kl_coeff * kl_old_mean

                loss = policy_loss + value_loss + prior_loss + kl_penalty

                with timing.add_time('update'):
                    # update the weights
                    self.optimizer.zero_grad()
                    loss.backward()

                    with timing.add_time('clip'):
                        torch.nn.utils.clip_grad_norm_(self.actor_critic.parameters(), self.cfg.max_grad_norm)

                    self.optimizer.step()
                    num_sgd_steps += 1

                self._after_optimizer_step()

                # collect and report summaries
                with_summaries = self._should_save_summaries()
                if with_summaries:
                    self.last_summary_written = self.train_step

                    stats = AttrDict()
                    grad_norm = sum(
                        p.grad.data.norm(2).item() ** 2
                        for p in self.actor_critic.parameters()
                        if p.grad is not None
                    ) ** 0.5
                    stats.grad_norm = grad_norm
                    stats.loss = loss
                    stats.value = result.values.mean()
                    stats.entropy = entropy
                    stats.kl_prior = kl_prior
                    stats.value_loss = value_loss
                    stats.prior_loss = prior_loss
                    stats.kl_coeff = self.kl_coeff
                    stats.kl_penalty = kl_penalty
                    stats.max_abs_logprob = torch.abs(mb.action_logits).max()
                    stats.avg_version = mb.policy_version.mean()

                    version_diff = self.policy_version - mb.policy_version
                    stats.version_diff_avg = version_diff.mean()
                    stats.version_diff_min = version_diff.min()
                    stats.version_diff_max = version_diff.max()

                    # we want this statistic for the last batch of the last epoch
                    for key, value in self.last_batch_stats.items():
                        stats[key] = value

                    for key, value in stats.items():
                        if isinstance(value, torch.Tensor):
                            stats[key] = value.detach()

                if self.cfg.early_stopping:
                    kl_99_th = np.percentile(kl_old.detach().cpu().numpy(), 99)
                    value_delta_99th = np.percentile(value_delta.detach().cpu().numpy(), 99)
                    if kl_99_th > self.cfg.target_kl * 5 or value_delta_99th > self.cfg.ppo_clip_value * 5:
                        log.info(
                            'Early stopping due to KL %.3f or value delta %.3f, epoch %d, step %d',
                            kl_99_th, value_delta_99th, epoch, num_sgd_steps,
                        )
                        early_stopping = True
                        break

        # adjust KL-penalty coefficient if KL divergence at the end of training is high
        if kl_old_mean > self.cfg.target_kl:
            self.kl_coeff *= 1.5
        elif kl_old_mean < self.cfg.target_kl / 2:
            self.kl_coeff /= 1.5
        self.kl_coeff = max(self.kl_coeff, 1e-6)

        self.last_batch_stats.kl_divergence = kl_old_mean
        self.last_batch_stats.kl_max = kl_old_max
        self.last_batch_stats.value_delta = value_delta_avg
        self.last_batch_stats.value_delta_max = value_delta_max
        self.last_batch_stats.fraction_clipped = fraction_clipped
        self.last_batch_stats.rnn_dist = rnn_dist
        self.last_batch_stats.ratio_mean = ratio_mean
        self.last_batch_stats.ratio_min = ratio_min
        self.last_batch_stats.ratio_max = ratio_max
        self.last_batch_stats.num_sgd_steps = num_sgd_steps

        value_delta_99th = np.percentile(value_delta.detach().cpu().numpy(), 99)
        log.info('Value delta avg, 99, max: %.3f, %.3f, %.3f', value_delta_avg, value_delta_99th, value_delta_max)

        return stats

    def _init_training(self, timing):
        with timing.timeit('init'):
            # initialize the Torch modules
            if self.cfg.seed is not None:
                log.info('Setting fixed seed %d', self.cfg.seed)
                torch.manual_seed(self.cfg.seed)
                np.random.seed(self.cfg.seed)

            torch.set_num_threads(1)  # TODO: experimental

            self.device = torch.device('cuda')
            self.actor_critic = ActorCritic(self.obs_space, self.action_space, self.cfg)
            self.actor_critic.to(self.device)
            self.actor_critic.share_memory()

            self.optimizer = torch.optim.Adam(
                self.actor_critic.parameters(),
                self.cfg.learning_rate,
                betas=(self.cfg.adam_beta1, self.cfg.adam_beta2),
                eps=self.cfg.adam_eps,
            )

            self._broadcast_weights()  # sync the very first version of the weights

        self.train_thread_initialized.set()

    def _process_training_data(self, data, timing, wait_stats=None):
        buffer, experience_size, samples, env_steps = data
        stats = dict(samples=samples, env_steps=env_steps)

        with timing.add_time('train'):
            if self.with_training:
                log.debug('Training policy %d on %d samples', self.policy_id, samples)
                train_stats = self._train(buffer, experience_size, timing)
                if train_stats is not None:
                    stats['train'] = train_stats

                    if wait_stats is not None:
                        wait_avg, wait_min, wait_max = wait_stats
                        stats['train']['wait_avg'] = wait_avg
                        stats['train']['wait_min'] = wait_min
                        stats['train']['wait_max'] = wait_max

                self._broadcast_weights()

        self.report_queue.put(stats)

    def _train_loop(self):
        timing = Timing()
        self._init_training(timing)

        wait_times = deque([], maxlen=20)

        while not self.terminate:
            with timing.timeit('train_wait'):
                data = safe_get(self.experience_buffer_queue)

            if self.terminate:
                break

            wait_times.append(timing.train_wait)
            wait_times_arr = np.asarray(wait_times)
            wait_avg = np.mean(wait_times_arr)
            wait_min, wait_max = wait_times_arr.min(), wait_times_arr.max()
            log.debug(
                'Training thread had to wait %.4f s for the new experience buffer (avg %.4f)',
                timing.train_wait, wait_avg,
            )

            wait_stats = (wait_avg, wait_min, wait_max)
            self._process_training_data(data, timing, wait_stats)

        log.info('Train loop timing: %s', timing)
        del self.actor_critic
        del self.device

    def _run(self):
        timing = Timing()

        plasma_client = plasma.connect(self.plasma_store_name)
        serialization_context = pa.default_serialization_context()

        rollouts = []

        if self.train_in_background:
            self.training_thread.start()
        else:
            self._init_training(timing)

        while not self.terminate:
            while True:
                try:
                    task_type, data = self.task_queue.get(timeout=0.01)
                    if task_type == TaskType.INIT:
                        self._init()
                    elif task_type == TaskType.TERMINATE:
                        self._terminate()
                        break
                    elif task_type == TaskType.TRAIN:
                        new_rollouts = plasma_client.get(data, -1, serialization_context=serialization_context)
                        rollouts.extend(new_rollouts)
                        rollouts = self._process_rollouts(rollouts, timing)

                        if not self.train_in_background:
                            while not self.experience_buffer_queue.empty():
                                training_data = self.experience_buffer_queue.get()
                                self._process_training_data(training_data, timing)

                    self.task_queue.task_done()
                except Empty:
                    break

        log.info('GPU learner timing: %s', timing)

        if self.train_in_background:
            self.experience_buffer_queue.put(None)
            self.training_thread.join()

    def init(self):
        self.task_queue.put((TaskType.INIT, None))
        self.task_queue.join()

    def close(self):
        self.task_queue.put((TaskType.TERMINATE, None))

    def join(self):
        self.process.join(timeout=5)


# WITHOUT TRAINING:
# [2019-11-27 22:06:02,056] Gpu learner timing: init: 3.1058, work: 0.0001
# [2019-11-27 22:06:02,059] Gpu worker timing: init: 2.7746, deserialize: 4.6964, to_device: 3.8011, forward: 14.2683, serialize: 8.4691, postprocess: 9.8058, policy_step: 32.8482, weight_update: 0.0005, gpu_waiting: 2.0623
# [2019-11-27 22:06:02,065] Gpu worker timing: init: 5.4169, deserialize: 3.6640, to_device: 3.1592, forward: 13.2836, serialize: 6.3964, postprocess: 7.6095, policy_step: 27.9706, weight_update: 0.0005, gpu_waiting: 1.8249
# [2019-11-27 22:06:02,067] Env runner 0: timing waiting: 0.8708, reset: 27.0515, save_policy_outputs: 0.0006, env_step: 26.0700, finalize: 0.3460, overhead: 1.1313, format_output: 0.3095, one_step: 0.0272, work: 36.5773
# [2019-11-27 22:06:02,079] Env runner 1: timing waiting: 0.8468, reset: 26.8022, save_policy_outputs: 0.0008, env_step: 26.1251, finalize: 0.3565, overhead: 1.1361, format_output: 0.3224, one_step: 0.0269, work: 36.6139

# WITH TRAINING 1 epoch:
# [2019-11-27 22:24:20,590] Gpu worker timing: init: 2.9078, deserialize: 5.5495, to_device: 5.6693, forward: 15.7285, serialize: 10.0113, postprocess: 13.4533, policy_step: 40.7373, weight_update: 0.0007, gpu_waiting: 2.0482
# [2019-11-27 22:24:20,596] Gpu worker timing: init: 4.8333, deserialize: 4.6056, to_device: 5.0975, forward: 14.8585, serialize: 8.0576, postprocess: 11.3531, policy_step: 36.2226, weight_update: 0.0006, gpu_waiting: 1.9836
# [2019-11-27 22:24:20,606] Env runner 1: timing waiting: 0.9328, reset: 27.9299, save_policy_outputs: 0.0005, env_step: 31.6222, finalize: 0.4432, overhead: 1.3904, format_output: 0.3692, one_step: 0.0309, work: 44.7151
# [2019-11-27 22:24:20,622] Env runner 0: timing waiting: 1.0276, reset: 27.5389, save_policy_outputs: 0.0009, env_step: 31.5377, finalize: 0.4614, overhead: 1.4103, format_output: 0.3564, one_step: 0.0269, work: 44.6398
# [2019-11-27 22:24:23,072] Gpu learner timing: init: 3.3635, last_values: 0.4506, gae: 3.5159, numpy: 0.6232, finalize: 4.6129, buffer: 6.4776, update: 16.3922, train: 26.0528, work: 37.2159
# [2019-11-27 22:24:52,618] Collected 1012576, FPS: 22177.3

# Env runner 0: timing waiting: 2.5731, reset: 5.0527, save_policy_outputs: 0.0007, env_step: 28.7689, overhead: 1.1565, format_inputs: 0.3170, one_step: 0.0276, work: 39.3452
# [2019-12-06 19:01:42,042] Env runner 1: timing waiting: 2.5900, reset: 4.9147, save_policy_outputs: 0.0004, env_step: 28.8585, overhead: 1.1266, format_inputs: 0.3087, one_step: 0.0254, work: 39.3333
# [2019-12-06 19:01:42,227] Gpu worker timing: init: 2.8738, weight_update: 0.0006, deserialize: 7.6602, to_device: 5.3244, forward: 8.1527, serialize: 14.3651, postprocess: 17.5523, policy_step: 38.8745, gpu_waiting: 0.5276
# [2019-12-06 19:01:42,232] Gpu learner timing: init: 3.3448, last_values: 0.2737, gae: 3.0682, numpy: 0.5308, finalize: 3.8888, buffer: 5.2451, forw_head: 0.2639, forw_core: 0.8289, forw_tail: 0.5334, clip: 4.5709, update: 12.0888, train: 19.6720, work: 28.8663
# [2019-12-06 19:01:42,723] Collected 1007616, FPS: 23975.2