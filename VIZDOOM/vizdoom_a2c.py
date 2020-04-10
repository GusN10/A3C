import itertools
import numpy as np
from skimage.transform import resize
import time
from datetime import datetime
import matplotlib.pyplot as plt
import torch
from torch import nn
import torch.nn.functional as F
from tqdm import trange
from vizdoom import DoomGame, Mode, ScreenFormat, ScreenResolution

from viz_utils import set_init, plotter_ep_rew, handleArguments, optimize, plotter_ep_time, confidence_intervall
from shared_adam import SharedAdam
import os
os.environ["OMP_NUM_THREADS"] = "1"


GAMMA = 0.9
MAX_EP = 30
frame_repeat = 12
resolution = (30, 45)
config_file_path = "deadly_corridor.cfg"


def initialize_vizdoom(config):
    game = DoomGame()
    game.load_config(config)
    if handleArguments().demo_mode:
        game.set_window_visible(True)
    else:
        game.set_window_visible(False)
    game.set_mode(Mode.PLAYER)
    game.set_screen_format(ScreenFormat.GRAY8)
    game.set_screen_resolution(ScreenResolution.RES_640X480)
    game.init()
    return game

def preprocess(img):
    return torch.from_numpy(resize(img, resolution).astype(np.float32))


def game_state(game):
    return preprocess(game.get_state().screen_buffer)


game = initialize_vizdoom(config_file_path)
statesize = (game_state(game).shape[0])
state = game_state(game)
n = game.get_available_buttons_size()
actions = [list(a) for a in itertools.product([0, 1], repeat=n)]

print("Current State:" , state, "\n")
print("Statesize:" , statesize, "\n")
print ("Action Size: ", n)
print("All possible Actions:", actions, "\n", "Total: ", len(actions))


class Net(nn.Module):
    def __init__(self, a_dim):
        super(Net, self).__init__()
        self.s_dim = 45
        self.a_dim = a_dim
        self.pi1 = nn.Linear(self.s_dim, 120)
        self.pi2 = nn.Linear(120, 360)
        self.pi3 = nn.Linear(360, a_dim)
        self.v1 = nn.Linear(self.s_dim, 120)
        self.v2 = nn.Linear(120, 360)
        self.v3 = nn.Linear(360, 1)

        #self.optimizer = torch.optim.SGD(self.parameters(), FLAGS.learning_rate)

        set_init([self.pi1, self.pi2, self.pi3, self.v1, self.v2, self.v3])
        self.distribution = torch.distributions.Categorical

    def forward(self, x):
        pi1 = F.relu(self.pi1(x))
        pi2 = F.relu(self.pi2(pi1))
        logits = self.pi3(pi2)
        v1 = F.relu(self.v1(x))
        v2 = F.relu(self.v2(v1))
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

        new_values = torch.zeros([len(v_t), 1], dtype=torch.float32)
        # Reshape Tensor of values
        for i in range(len(v_t)):
            for j in range(30):
                values[i][0] += values[i+j][0]
            new_values[i][0] =  values[i][0]

        td = v_t - new_values
        c_loss = td.pow(2)
        new_logits = torch.zeros([len(v_t), 128], dtype=torch.float32)
        # Reshape Tensor of logits
        for i in range(len(logits[0])):
            countrow = 0
            for j in range(len(logits)):
                logits[countrow][i] += logits[j][i]
                if j % 30 == 0:
                    new_logits[countrow][i] = logits[countrow][i]
                    countrow += 1
        probs = F.softmax(new_logits, dim=1)
        m = self.distribution(probs)
        exp_v = m.log_prob(a) * td.detach().squeeze()
        a_loss = -exp_v
        total_loss = (c_loss + a_loss).mean()
        return total_loss



if __name__ == '__main__':

    print ("Starting A2C Agent for Vizdoom-DeadlyCorridor")
    time.sleep(3)

    timedelta_sum = datetime.now()
    timedelta_sum -= timedelta_sum
    fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True)
    action = []

    for i in range(3):
        starttime = datetime.now()

        # load global network
        if handleArguments().load_model:
            model = Net(len(actions))
            model = torch.load("./doom_save_model/a2c_doom.pt")
            model.eval()
        else:
            model = Net(len(actions))

        opt = SharedAdam(model.parameters(), lr=0.005, betas=(0.92, 0.999))  # global optimizer

        # Global variables for episodes
        durations = []
        scores = []
        global_ep, global_ep_r = 1, 0.
        name = 'w00'
        total_step = 1
        stop_processes = False

        while global_ep < MAX_EP:
            game.new_episode()
            buffer_s, buffer_a, buffer_r = [], [], []
            ep_r = 0.
            while True:
                # Initialize stopwatch for average episode duration
                start = time.time()
                done = False

                a = model.choose_action(state)
                action.append(a)
                r = game.make_action(actions[a], frame_repeat)

                if game.is_episode_finished():
                    done = True
                else:
                    s_ = game_state(game)

                ep_r += r
                buffer_a.append(a)
                buffer_s.append(state)
                buffer_r.append(r)

                if done or ep_r == 50:  # update network
                    # sync
                    optimize(opt, model, done, s_, buffer_s, buffer_a, buffer_r, GAMMA)
                    print("Acion_array:", action)
                    game.get_total_reward()
                    buffer_s, buffer_a, buffer_r = [], [], []

                    global_ep += 1
                    if global_ep_r == 0.:
                        global_ep_r = ep_r
                    else:
                        global_ep_r = global_ep_r * 0.99 + ep_r * 0.01

                    end = time.time()
                    duration = end - start
                    durations.append(duration)

                    print("w00 Ep:", global_ep, "| Ep_r: %.0f" % global_ep_r, "| Duration:", round(duration, 5))
                    scores.append(int(global_ep_r))

                    # TODO: check for reasonable reward and adjust
                    if handleArguments().load_model:
                        if np.mean(scores[-min(10, len(scores)):]) >= 0 and global_ep >= 10:
                            stop_processes = True
                    else:
                        if np.mean(scores[-min(10, len(scores)):]) >= 0 and global_ep >= 10:
                            stop_processes = True
                    break

                s = s_
                total_step += 1


        # TODO: check for reasonable reward and adjust
        if np.mean(scores[-min(10, len(scores)):]) >= 0 and not handleArguments().load_model and global_ep >= 10:
            print("Save model")
            torch.save(model, "./doom_save_model/a2c_doom.pt")
        elif handleArguments().load_model:
            print ("Testing! No need to save model.")
        else:
            print("Failed to train agent. Model was not saved")

        endtime = datetime.now()
        timedelta = endtime - starttime
        print("Number of Episodes: ", global_ep, " | Finished within: ", timedelta)
        timedelta_sum += timedelta/3

        # Get results for confidence intervall
        #if handleArguments().load_model:
         #   confidence_intervall(action, True)
        #else:
         #   confidence_intervall(action)

        # Plot results
        plotter_ep_time(ax1, durations)
        plotter_ep_rew(ax2, scores)

    font = {'family': 'serif',
            'color': 'darkred',
            'weight': 'normal',
            'size': 8
            }
    plt.text(0, 250, f"Average Training Duration: {timedelta_sum}", fontdict=font)
    plt.title("Vanilla A2C-Vizdoom", fontsize = 16)
    plt.show()

    game.close()


