"""
Reinforcement Learning (A3C) using Pytroch + multiprocessing.
The most simple implementation for continuous action.

"""

import torch
import torch.nn as nn
from utils import v_wrap, set_init, push_and_pull, record, plotter_ep_rew, handleArguments, plotter_ep_time, confidence_intervall
import matplotlib.pyplot as plt
import torch.nn.functional as F
import torch.multiprocessing as mp
from shared_adam import SharedAdam
import numpy as np
import gym
import time
from datetime import datetime
import sys
import os
os.environ["OMP_NUM_THREADS"] = "1"

UPDATE_GLOBAL_ITER = 20
GAMMA = 0.9
MAX_EP = 3000



env = gym.make('CartPole-v0').unwrapped
N_S = env.observation_space.shape[0]
N_A = env.action_space.n


class Net(nn.Module):
    def __init__(self, s_dim, a_dim):
        super(Net, self).__init__()
        self.s_dim = s_dim
        self.a_dim = a_dim
        self.pi1 = nn.Linear(s_dim, 24)
        self.pi12 = nn.Linear(24, 48)
        self.pi2 = nn.Linear(48, 24)
        self.pi3 = nn.Linear(24, a_dim)
        self.v2 = nn.Linear(48, 24)
        self.v3 = nn.Linear(24, 1)
        set_init([self.pi1, self.pi12, self.pi2, self.pi3, self.v2, self.v3])
        self.distribution = torch.distributions.Categorical

    def forward(self, x):
        pi1 = F.relu(self.pi1(x))
        pi12 = F.relu(self.pi12(pi1))
        pi2 = F.relu(self.pi2(pi12))
        logits = self.pi3(pi2)
        v2 = F.relu(self.v2(pi12))
        values = self.v3(v2)
        return logits, values

    def set_init(layers):
        for layer in layers:
            nn.init.xavier_uniform_(layer.weight, nn.init.calculate_gain('relu'))
            nn.init.xavier_uniform_(layer.bias, nn.init.calculate_gain('relu'))

    def choose_action(self, s):
        self.eval()
        logits, _ = self.forward(s)
        prob = F.softmax(logits, dim=1).data
        m = self.distribution(prob)
        return m.sample().numpy()[0]

    def loss_func(self, s, a, v_t):
        self.train()
        logits, values = self.forward(s)
        td = v_t - values
        c_loss = td.pow(2)
        
        probs = F.softmax(logits, dim=1)
        m = self.distribution(probs)
        exp_v = m.log_prob(a) * td.detach().squeeze()
        a_loss = -exp_v
        total_loss = (c_loss + a_loss).mean()
        return total_loss


class Worker(mp.Process):
    def __init__(self, gnet, opt, global_ep, global_ep_r, res_queue, time_queue, action_queue, name):
        super(Worker, self).__init__()
        self.name = 'w%02i' % name
        self.g_ep, self.g_ep_r, self.res_queue = global_ep, global_ep_r, res_queue
        self.gnet, self.opt = gnet, opt
        self.lnet = Net(N_S, N_A)           # local network
        self.env = gym.make('CartPole-v0').unwrapped
        self.time_queue, self.action_queue = time_queue, action_queue

    def run(self):
        total_step = 1
        stop_processes = False
        scores = []

        while self.g_ep.value < MAX_EP and stop_processes is False:
            s = self.env.reset()
            buffer_s, buffer_a, buffer_r = [], [], []
            ep_r = 0.
            while True:
                start = time.time()
                if self.name == 'w00' and handleArguments().demo_mode:
                    self.env.render()
                a = self.lnet.choose_action(v_wrap(s[None, :]))
                s_, r, done, _ = self.env.step(a)
                if done: r = -1
                ep_r += r
                buffer_a.append(a)
                buffer_s.append(s)
                buffer_r.append(r)


                if total_step % UPDATE_GLOBAL_ITER == 0 or done or ep_r == 600:  # update global and assign to local net
                    # sync
                    push_and_pull(self.opt, self.lnet, self.gnet, done, s_, buffer_s, buffer_a, buffer_r, GAMMA)
                    buffer_s, buffer_a, buffer_r = [], [], []

                    if done or ep_r == 600:  # done and print information
                        end = time.time()
                        time_done = end - start
                        record(self.g_ep, self.g_ep_r, ep_r, self.res_queue, self.time_queue, time_done, a, self.action_queue, self.name)
                        scores.append(int(self.g_ep_r.value))
                        if handleArguments().load_model:
                            if np.mean(scores[-min(100, len(scores)):]) >= 500 and self.g_ep.value >= 100:
                                stop_processes = True
                        else:
                            if np.mean(scores[-min(mp.cpu_count(), len(scores)):]) >= 500 and self.g_ep.value >= mp.cpu_count():
                                stop_processes = True


                        break


                s = s_
                total_step += 1
        self.time_queue.put(None)
        self.res_queue.put(None)
        self.action_queue.put(None)


if __name__ == "__main__":
    # load global network
    print("STARTING A3C AGENT FOR CARTPOLE-V0")
    time.sleep(3)
    timedelta_sum = datetime.now()
    timedelta_sum -= timedelta_sum
    fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True)

    # Reinitialize Agent to make 5 trials
    for i in range(3):
        starttime = datetime.now()
        if handleArguments().load_model:
            gnet = Net(N_S, N_A)
            gnet = torch.load("./save_model/a3c_cart_comb.pt")
            gnet.eval()
        else:
            gnet = Net(N_S, N_A)

        # share the global parameters in multiprocessing
        gnet.share_memory()

        # global optimizer
        opt = SharedAdam(gnet.parameters(), lr=0.003, betas=(0.92, 0.999))

        global_ep, global_ep_r, res_queue, time_queue, action_queue = mp.Value('i', 0), mp.Value('d', 0.), mp.Queue(), mp.Queue(), mp.Queue()

        # parallel training
        if handleArguments().load_model:
            workers = [Worker(gnet, opt, global_ep, global_ep_r, res_queue, time_queue, action_queue, i) for i in range(1)]
            [w.start() for w in workers]
        else:
            workers = [Worker(gnet, opt, global_ep, global_ep_r, res_queue, time_queue, action_queue, i) for i in range(mp.cpu_count())]
            [w.start() for w in workers]

        # record episode-reward and duration-episode to plot
        res = []
        durations = []
        actions = []
        while True:
            r = res_queue.get()
            t = time_queue.get()
            a = action_queue.get()
            if r is not None:
                res.append(r)
                durations.append(t)
                actions.append(a)
            else:
                break

        [w.join() for w in workers]

        if np.mean(res[-min(mp.cpu_count(), len(res)):]) >= 300:
            print("Save model")
            torch.save(gnet, "./save_model/a3c_cart_comb.pt")
        else:
            print("Failed to train agent. Model was not saved")

        endtime = datetime.now()
        timedelta = endtime - starttime
        print("Number of Episodes: ", global_ep.value, " | Finished within: ", timedelta)
        timedelta_sum += timedelta/3

        # Get results for confidence intervall
        confidence_intervall(actions)

        # Plot results after each run
        plotter_ep_time(ax1, durations)
        plotter_ep_rew(ax2, res)

    # Finish plots
    font = {'family': 'serif',
            'color': 'darkred',
            'weight': 'normal',
            'size': 8,
            }
    plt.text(0, 650, f"Average Training Duration: {timedelta_sum}", fontdict=font)
    plt.title("A3C-Cartpole (shared NN)")
    plt.show()

    sys.exit()

