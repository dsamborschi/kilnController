#!/usr/bin/python

import os
import sys
import logging
import json

import datetime
import traceback

try:
    sys.dont_write_bytecode = True
    import config

    sys.dont_write_bytecode = False
except:
    print("Could not import config file.")
    print("Copy config.py.EXAMPLE to config.py and adapt it for your setup.")
    exit(1)

logging.basicConfig(level=config.log_level, format=config.log_format)
log = logging.getLogger("kilncontrollerd")
log.info("Starting kilncontrollerd")

script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, script_dir + '/lib/')
profile_path = os.path.join(script_dir, "storage", "profiles")


from oven2 import Oven, Profile
from ovenWatcher import OvenWatcher

from utils import millis


def get_profiles():
    try:
        profile_files = os.listdir(profile_path)
    except:
        profile_files = []
    profiles = []
    for filename in profile_files:
        with open(os.path.join(profile_path, filename), 'r') as f:
            profiles.append(json.load(f))
    return profiles


def main():
    ip = config.listening_ip
    port = config.listening_port
    log.info("listening on %s:%d" % (ip, port))

    log.info("SIMULATE command received")
    profiles = []
    profiles = get_profiles()

    profile_json = json.dumps(profiles[0])
    profile = Profile(profile_json)

    oven = Oven(simulate=True, time_step=10)
    oven.run_profile(profile)






if __name__ == "__main__":
    main()
