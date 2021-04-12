import threading
import time
import random
import datetime
import logging
import json

import config

from utils import millis

log = logging.getLogger(__name__)

# max number of segments
max_number_segs = 20
# Hold time for each segment (min).  This starts after it reaches target temp.
# seg_hold[max_number_segs]
# Rate of temp change for each segment (deg/hr).
# seg_ramp[max_number_segs]
# Target temp for each segment (degrees).
# seg_temp[max_number_segs]
# Current segment number running in firing schedule.  0 means a schedule hasn't been selected yet.
seg_num = 0
# Current segment phase.  0 = ramp.  1 = hold.
seg_phase = 0
# This is how close the temp reading needs to be to the set point to shift to the hold phase (degrees).  Set to zero or a positive integer.
temp_range = 2
pid_cycle = 2500
try:
    if config.max31855 + config.max6675 + config.max31855spi > 1:
        log.error("choose (only) one converter IC")
        exit()
    if config.max31855:
        from max31855 import MAX31855, MAX31855Error

        log.info("import MAX31855")
    if config.max31855spi:
        import Adafruit_GPIO.SPI as SPI
        from max31855spi import MAX31855SPI, MAX31855SPIError

        log.info("import MAX31855SPI")
        spi_reserved_gpio = [7, 8, 9, 10, 11]

        if config.gpio_heat in spi_reserved_gpio:
            raise Exception("gpio_heat pin %s collides with SPI pins %s" % (config.gpio_heat, spi_reserved_gpio))
    if config.max6675:
        from max6675 import MAX6675, MAX6675Error

        log.info("import MAX6675")
    sensor_available = True
except ImportError:
    log.exception("Could not initialize temperature sensor, using dummy values!")
    sensor_available = False

try:
    import RPi.GPIO as GPIO

    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(config.gpio_heat, GPIO.OUT)

    gpio_available = True
except ImportError:
    msg = "Could not initialize GPIOs, oven operation will only be simulated!"
    log.warning(msg)
    gpio_available = False


class Oven(threading.Thread):
    STATE_IDLE = "IDLE"
    STATE_RUNNING = "RUNNING"

    def __init__(self, simulate=False, time_step=config.sensor_time_wait):
        threading.Thread.__init__(self)
        self.profile = None
        self.start_time = 0
        self.runtime = 0
        self.target = 0
        self.state = Oven.STATE_IDLE
        self.daemon = True
        self.simulate = simulate
        self.time_step = time_step
        self.heat = 0
        self.reset()
        if simulate:
            self.temp_sensor = TempSensorSimulate(self, 0.5, self.time_step)
        if sensor_available:
            self.temp_sensor = TempSensorReal(self.time_step)
        else:
            self.temp_sensor = TempSensorSimulate(self,
                                                  self.time_step,
                                                  self.time_step)
        self.temp_sensor.start()
        self.start()

    def reset(self):
        self.profile = None
        self.start_time = 0
        self.runtime = 0
        self.target = 0
        self.state = Oven.STATE_IDLE
        GPIO.output(config.gpio_heat, GPIO.LOW)
        self.pid = PID(ki=config.pid_ki, kd=config.pid_kd, kp=config.pid_kp)

    def run_profile(self, profile):
        log.info("Running profile %s" % profile.name)
        self.profile = profile
        self.profile.running = True
        # Ramp-hold init
        self.profile.rampStart = millis()
        self.profile.pidStart = millis()
        self.profile.segNum = 1

        self.state = Oven.STATE_RUNNING
        self.start_time = datetime.datetime.now()
        log.info("Starting")

    def abort_run(self):
        self.reset()

    def run(self):
        temperature_count = 0
        last_temp = 0
        pid = 0

        while True:

            if self.state == Oven.STATE_RUNNING:
                if self.simulate:
                    self.runtime += 0.5
                else:
                    runtime_delta = datetime.datetime.now() - self.start_time
                    self.runtime = runtime_delta.total_seconds()

                if millis() - self.profile.pidStart >= pid_cycle:
                    self.profile.pidStart = millis()
                    self.target = self.profile.get_target_temperature2(config.temp_scale)

                pid = self.pid.compute(self.target, self.temp_sensor.temperature)

                log.info("running at %.1f deg F (Target: %.1f) , heat %.2f, PID %.1f" % (
                    self.temp_sensor.temperature, self.target, self.heat, pid))

                # Capture the last temperature value. This must be done before set_heat, since there is a sleep
                last_temp = self.temp_sensor.temperature
                self.set_heat(pid)
                # Update the schedule segment
                self.profile.update_seg(self.temp_sensor.temperature)

                if self.profile.finished():
                    self.reset()

            if pid > 0:
                time.sleep(self.time_step * (1 - pid))
                log.info("pid is %.1f. Sleep for %.2f" % (pid, self.time_step * (1 - pid)))
            else:
                log.info("pid is %.1f. Sleep for %.2f" % (pid, self.time_step))
                time.sleep(self.time_step)

    def set_heat(self, value):
        if value > 0:
            self.heat = 1.0
            if gpio_available:
                GPIO.output(config.gpio_heat, GPIO.HIGH)
                time.sleep(self.time_step * value)
                GPIO.output(config.gpio_heat, GPIO.LOW)
        else:
            self.heat = 0.0
            if gpio_available:
                GPIO.output(config.gpio_heat, GPIO.LOW)

    def set_heat2(self, value, pidstart):
        if gpio_available:
            if value >= millis() - pidstart:
                self.heat = 1.0
                GPIO.output(config.gpio_heat, GPIO.HIGH)
                log.info("Heat is ON")
            else:
                self.heat = 0.0
                GPIO.output(config.gpio_heat, GPIO.LOW)
                log.info("Heat is OFF")

    def get_state(self):
        state = {
            'runtime': self.runtime,
            'temperature': self.temp_sensor.temperature,
            'target': self.target,
            'state': self.state,
            'heat': self.heat,
            'totaltime': self.profile.get_duration() if self.profile else 0,

        }
        return state


class TempSensor(threading.Thread):
    def __init__(self, time_step):
        threading.Thread.__init__(self)
        self.daemon = True
        self.temperature = 0
        self.time_step = time_step


class TempSensorReal(TempSensor):
    def __init__(self, time_step):
        TempSensor.__init__(self, time_step)
        if config.max6675:
            log.info("init MAX6675")
            self.thermocouple = MAX6675(config.gpio_sensor_cs,
                                        config.gpio_sensor_clock,
                                        config.gpio_sensor_data,
                                        config.temp_scale)

        if config.max31855:
            log.info("init MAX31855")
            self.thermocouple = MAX31855(config.gpio_sensor_cs,
                                         config.gpio_sensor_clock,
                                         config.gpio_sensor_data,
                                         config.temp_scale)

        if config.max31855spi:
            log.info("init MAX31855-spi")
            self.thermocouple = MAX31855SPI(spi_dev=SPI.SpiDev(port=0, device=config.spi_sensor_chip_id))

    def run(self):
        while True:
            try:
                self.temperature = self.thermocouple.get()
            except Exception:
                log.exception("problem reading temp")
            time.sleep(self.time_step)


class TempSensorSimulate(TempSensor):
    def __init__(self, oven, time_step, sleep_time):
        TempSensor.__init__(self, time_step)
        self.oven = oven
        self.sleep_time = sleep_time

    def run(self):
        t_env = config.sim_t_env
        c_heat = config.sim_c_heat
        c_oven = config.sim_c_oven
        p_heat = config.sim_p_heat
        R_o_nocool = config.sim_R_o_nocool
        R_o_cool = config.sim_R_o_cool
        R_ho_noair = config.sim_R_ho_noair
        R_ho_air = config.sim_R_ho_air

        t = t_env  # deg C  temp in oven
        t_h = t  # deg C temp of heat element
        while True:
            # heating energy
            Q_h = p_heat * self.time_step * self.oven.heat

            # temperature change of heat element by heating
            t_h += Q_h / c_heat

            R_ho = R_ho_noair

            # energy flux heat_el -> oven
            p_ho = (t_h - t) / R_ho

            # temperature change of oven and heat el
            t += p_ho * self.time_step / c_oven
            t_h -= p_ho * self.time_step / c_heat

            # energy flux oven -> env

            p_env = (t - t_env) / R_o_nocool

            # temperature change of oven by cooling to env
            t -= p_env * self.time_step / c_oven
            log.debug("energy sim: -> %dW heater: %.0f -> %dW oven: %.0f -> %dW env" % (
                int(p_heat * self.oven.heat), t_h, int(p_ho), t, int(p_env)))
            self.temperature = t

            time.sleep(self.sleep_time)


class Profile:
    def __init__(self, json_data):
        obj = json.loads(json_data)
        self.name = obj["name"]
        self.type = obj["type"]
        self.timeDiffs = [(0, 0)]
        self.segRamps = []
        self.segTemps = []
        self.segHolds = []
        self.segPhase = 0
        # Exact time the hold phase of the segment started (ms).  Based on now().
        self.holdStart = 0
        self.segNum = 0
        self.rampStart = 0
        self.lastTemp = 0

        if self.type == "profile":
            self.data = [(0, 0)] + sorted(obj["data"])
            for i in range(1, len(self.data)):
                self.timeDiffs.append((self.data[i][0] - self.data[i - 1][0], self.data[i][1]))
        if self.type == "ramp-hold":
            self.data = obj["data"]
            for i in range(0, len(self.data)):
                self.segRamps.append(self.data[i][0])
                self.segTemps.append(self.data[i][1])
                self.segHolds.append(self.data[i][2])

            # Fix Ramp values so it will show the correct sign (+/-).  This will help to determine when to start hold.
            for i in range(0, len(self.segRamps)):
                self.segRamps[i] = abs(self.segRamps[i])
                if i >= 1:
                    if self.segTemps[i] < self.segTemps[i - 1]:
                        self.segRamps[i] = -self.segRamps[i]

        self.currentState = 1
        self.numStates = len(self.timeDiffs)
        self.numSegments = len(self.segTemps)
        self.running = False
        self.lastStateChange = 0
        self.totalTime = self.data[-1][0]
        self.overtime = 0
        log.info(str(self.timeDiffs))
        log.info(str(self.totalTime))

    def update_seg(self, temp_sensor):
        # Start the hold phase
        if ((self.segPhase == 0 and self.segRamps[self.segNum - 1] < 0 and temp_sensor <= (
                self.segTemps[self.segNum - 1] + temp_range)) or
                (self.segPhase == 0 and self.segRamps[self.segNum - 1] >= 0 and temp_sensor >= (
                        self.segTemps[self.segNum - 1] - temp_range))):
            self.segPhase = 1
            self.holdStart = millis()

        # Go to the next segment
        if self.segPhase == 1 and (millis() - self.holdStart >= self.segHolds[self.segNum - 1] * 60000):
            self.segNum = self.segNum + 1
            self.segPhase = 0
            self.rampStart = millis()

        # Check if complete
        if self.segNum - 1 > self.numSegments:
            self.running = False

    def get_target_temperature2(self, temp_scale):
        # Get the last target temperature
        if self.segNum == 1:  # Set to room temperature for first segment
            if temp_scale == 'c':
                self.lastTemp = 24

            if temp_scale == 'f':
                self.lastTemp = 75

        else:
            self.lastTemp = self.segTemps[self.segNum - 2]

        # Calculate the new set point value.  Don't set above / below target temp
        if self.segPhase == 0:
            ramp_hours = (millis() - self.rampStart) / 3600000.0
            calc_set_point = self.lastTemp + (self.segRamps[self.segNum - 1] * ramp_hours)  # Ramp
            if self.segRamps[self.segNum - 1] >= 0 and calc_set_point >= self.segTemps[self.segNum - 1]:
                calc_set_point = self.segTemps[self.segNum - 1]

            if self.segRamps[self.segNum - 1] < 0 and calc_set_point <= self.segTemps[self.segNum - 1]:
                calc_set_point = self.segTemps[self.segNum - 1]

        else:
            calc_set_point = self.segTemps[self.segNum - 1]  # Hold

        return calc_set_point

    def finished(self):
        return not self.running

    def get_duration(self):
        return self.totalTime + self.overtime

    def get_surrounding_points(self):
        prev_point = None
        next_point = None

        if self.currentState < self.numStates:
            prev_point = self.timeDiffs[self.currentState - 1]
            next_point = self.timeDiffs[self.currentState]

        return prev_point, next_point

    def is_rising(self):
        (prev_point, next_point) = self.get_surrounding_points()
        if prev_point and next_point:
            return prev_point[1] < next_point[1]
        else:
            return False

    def get_target_temperature(self, time, temperature):
        relativeTime = time - self.lastStateChange
        minimumTime = self.timeDiffs[self.currentState][0]

        if relativeTime < minimumTime:
            targetTemp = self.get_intermediate_temperature(relativeTime)
        else:
            if self.check_target(temperature):
                # phase transition
                self.currentState += 1
                self.lastStateChange = time

                self.totalTime += self.overtime
                self.overtime = 0

                targetTemp = self.get_intermediate_temperature(0)

                if self.currentState == self.numStates:
                    self.running = False
            else:
                targetTemp = self.timeDiffs[self.currentState][1]
                self.overtime = relativeTime - minimumTime

        return targetTemp

    def get_intermediate_temperature(self, relativeTime):
        (prev_point, next_point) = self.get_surrounding_points()

        if next_point[0] == 0:
            targetTemp = next_point[1]
        else:
            incl = float(next_point[1] - prev_point[1]) / float(next_point[0])
            targetTemp = prev_point[1] + (relativeTime * incl)

        return targetTemp


"""
Tests to see if the target temperature has been acquired.
"""


class PID():
    def __init__(self, ki=1, kp=1, kd=1):
        self.ki = ki
        self.kp = kp
        self.kd = kd
        self.lastNow = datetime.datetime.now()
        self.iterm = 0
        self.lastErr = 0

    def compute(self, setpoint, ispoint):
        now = datetime.datetime.now()
        timeDelta = (now - self.lastNow).total_seconds()

        error = float(setpoint - ispoint)
        self.iterm += (error * timeDelta * self.ki)
        self.iterm = sorted([-1, self.iterm, 1])[1]
        dErr = (error - self.lastErr) / timeDelta

        output = self.kp * error + self.iterm + self.kd * dErr
        output = sorted([-1, output, 1])[1]
        self.lastErr = error
        self.lastNow = now

        return output
