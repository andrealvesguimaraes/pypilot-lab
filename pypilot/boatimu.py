#!/usr/bin/env python
#
#   Copyright (C) 2020 Sean D'Epagnier
#
# This Program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation; either
# version 3 of the License, or (at your option) any later version.  

# Boat imu is built on top of RTIMU

# it is an enhanced imu with special knowledge of boat dynamics
# giving it the ability to auto-calibrate the inertial sensors

from __future__ import print_function
import os, sys
import time, math, multiprocessing, select

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import calibration_fit, vector, quaternion
from pypilot.server import pypilotServer
from pypilot.pipeserver import pypilotPipeServer, NonBlockingPipe
from pypilot.values import *

try:
  import RTIMU
except ImportError:
  RTIMU = False
  print('RTIMU library not detected, please install it')

def imu_process(pipe, cal_pipe, accel_cal, compass_cal, gyrobias, period):
    if not RTIMU:
      while True:
        time.sleep(10)
  
    #print 'imu on', os.getpid()
    if os.system('sudo chrt -pf 99 %d 2>&1 > /dev/null' % os.getpid()):
      print('warning, failed to make imu process realtime')

    #os.system("sudo renice -10 %d" % os.getpid())
    SETTINGS_FILE = "RTIMULib"
    s = RTIMU.Settings(SETTINGS_FILE)
    s.FusionType = 1
    s.CompassCalValid = False

    s.CompassCalEllipsoidOffset = tuple(compass_cal[:3])  
    s.CompassCalEllipsoidValid = True
    s.MPU925xAccelFsr = 0 # +- 2g
    s.MPU925xGyroFsr = 0 # +- 250 deg/s
    # compass noise by rate 10=.043, 20=.033, 40=.024, 80=.017, 100=.015
    rate = 100
    s.MPU925xGyroAccelSampleRate = rate
    s.MPU925xCompassSampleRate = rate

    s.AccelCalValid = True
    if accel_cal:
      s.AccelCalMin = tuple(map(lambda x : x - accel_cal[3], accel_cal[:3]))
      s.AccelCalMax = tuple(map(lambda x : x + accel_cal[3], accel_cal[:3]))
    else:
      s.AccelCalMin = (-1, -1, -1)
      s.AccelCalMax = (1, 1, 1)

    s.GyroBiasValid = True
    if gyrobias:
      s.GyroBias = tuple(map(math.radians, gyrobias))
    else:
      s.GyroBias = (0, 0, 0)

    s.KalmanRk, s.KalmanQ = .002, .001
#    s.KalmanRk, s.KalmanQ = .0005, .001

    while True:
      print("Using settings file " + SETTINGS_FILE + ".ini")
      s.IMUType = 0 # always autodetect imu
      rtimu = RTIMU.RTIMU(s)
      if rtimu.IMUName() == 'Null IMU':
        print('no IMU detected... try again')
        time.sleep(1)
        continue
      
      print("IMU Name: " + rtimu.IMUName())

      if not rtimu.IMUInit():
        print("ERROR: IMU Init Failed, no inertial data available")
        time.sleep(1)
        continue

      # this is a good time to set any fusion parameters
      rtimu.setSlerpPower(.01)
      rtimu.setGyroEnable(True)
      rtimu.setAccelEnable(True)
      rtimu.setCompassEnable(True)

      poll_interval = rtimu.IMUGetPollInterval()
      time.sleep(.1)

      cal_poller = select.poll()
      cal_poller.register(cal_pipe, select.POLLIN)

      avggyro = [0, 0, 0]
      compass_calibration_updated = False

      while True:
        t0 = time.time()
        if not rtimu.IMURead():
            print('failed to read IMU!!!!!!!!!!!!!!')
            pipe.send(False)
            break # reinitialize imu
         
        data = rtimu.getIMUData()
        data['accel.residuals'] = list(rtimu.getAccelResiduals())
        data['gyrobias'] = s.GyroBias
        #data['timestamp'] = t0 # imu timestamp is perfectly accurate
        
        if compass_calibration_updated:
          data['compass_calibration_updated'] = True
          compass_calibration_updated = False

        pipe.send(data, False)

        # see if gyro is out of range, sometimes the sensors read
        # very high gyro readings and the sensors need to be reset by software
        # this is probably a bug in the underlying driver with fifo misalignment
        d = .05*period # filter constant
        for i in range(3): # filter gyro vector
            avggyro[i] = (1-d)*avggyro[i] + d*data['gyro'][i]
        if vector.norm(avggyro) > .8: # 55 degrees/s
            print('too high standing gyro bias, resetting sensors', data['gyro'], avggyro)
            break
        # detects the problem even faster:
        if any(map(lambda x : abs(x) > 1000, data['compass'])):
            print('compass out of range, resetting', data['compass'])
            break
        
        if cal_poller.poll(0):
          r = cal_pipe.recv()

          #print('[imu process] new cal', new_cal)
          if r[0] == 'accel':
            s.AccelCalValid = True
            b, t = r[1][0][:3], r[1][0][3]
            s.AccelCalMin = b[0] - t, b[1] - t, b[2] - t
            s.AccelCalMax = b[0] + t, b[1] + t, b[2] + t
          elif r[0] == 'compass':
            compass_calibration_updated = True
            s.CompassCalEllipsoidValid = True
            s.CompassCalEllipsoidOffset = tuple(r[1][0][:3])
          #rtimu.resetFusion()
        
        dt = time.time() - t0
        t = period - dt

        if t > 0 and t < period:
          time.sleep(t)
        else:
          print('imu process failed to keep time', t)

class LoopFreqValue(Value):
    def __init__(self, name, initial):
        super(LoopFreqValue, self).__init__(name, initial)
        self.loopc = 0
        self.t0 = time.time()

    def strobe(self):
        self.loopc += 1
        if self.loopc == 10:
            t1 = time.time()
            self.set(self.loopc/(t1-self.t0))
            self.t0 = t1
            self.loopc = 0

def readable_timespan(total):
    mods = [('s', 1), ('m', 60), ('h', 60), ('d', 24), ('y', 365.24)]          
    def loop(i, mod):
        if i == len(mods) or (int(total / (mods[i][1]*mod)) == 0 and i > 0):
            return ''
        if i < len(mods) - 1:
            div = mods[i][1]*mods[i+1][1]*mod
            t = int(total%int(div))
        else:
            t = total
        return loop(i+1, mods[i][1]*mod) + (('%d' + mods[i][0] + ' ') % (t/(mods[i][1]*mod)))
    return loop(0, 1)

class TimeValue(StringValue):
    def __init__(self, name, **kwargs):
        super(TimeValue, self).__init__(name, 0, **kwargs)
        self.lastupdate_value = 0
        self.lastage_value = -100
        self.stopped = True
        self.total = self.value
        
    def reset(self):
        self.lastupdate_value = 0
        self.total = 0
        self.start = time.time()
        self.set(0)

    def update(self):
        t = time.time()
        if self.stopped:
            self.stopped = False
            self.start = t

        self.value = self.total + t - self.start
        if abs(self.value - self.lastupdate_value) > 1:
          self.lastupdate_value = self.value
          self.send()

    def stop(self):
      if self.stopped:
        return
      self.total += time.time() - self.start
      self.stopped = True

    def get_pypilot(self):
        if abs(self.value - self.lastage_value) > 1: # to reduce cpu, if the time didn't change by a second
            self.lastage_value = self.value
            self.lastage = readable_timespan(self.value)
        return '{"' + self.name + '": {"value": "' + self.lastage + '"}}'
      
class AgeValue(StringValue):
    def __init__(self, name, **kwargs):
        super(AgeValue, self).__init__(name, time.time(), **kwargs)
        self.dt = max(0, time.time() - self.value)
        self.lastupdate_value = -1
        self.lastage = ''

    def reset(self):
        self.set(time.time())

    def update(self):
        t = time.time()
        if abs(t - self.lastupdate_value) > 1:
          self.lastupdate_value = t
          self.send()

    def get_pypilot(self):
        dt = max(0, time.time() - self.value)
        if abs(dt - self.dt) > 1:
            self.dt = dt
            self.lastage = readable_timespan(dt)
        return '{"' + self.name + '": {"value": "' + self.lastage + '"}}'

class QuaternionValue(ResettableValue):
    def __init__(self, name, initial, **kwargs):
      super(QuaternionValue, self).__init__(name, initial, **kwargs)

    def set(self, value):
      if value:
        value = quaternion.normalize(value)
      super(QuaternionValue, self).set(value)


class CalibrationProperty(RoundedValue):
  def __init__(self, name, server, default):
    self.default = default
    self.client_can_set = True
    super(CalibrationProperty, self).__init__(name+'.calibration', default, persistent=True)

  def set(self, value):
    if not value:
      value = self.default
    try:
      if self.value and self.locked.value:
        return
      self.age.reset()
    except:
      pass # startup before locked is initiated
    super(CalibrationProperty, self).set(value)

def heading_filter(lp, a, b):
    if not a:
        return b
    if not b:
        return a
    if a - b > 180:
        a -= 360
    elif b - a > 180:
        b -= 360
    result = lp*a + (1-lp)*b
    if result < 0:
        result += 360
    return result

class BoatIMU(object):
  def __init__(self, server, *args, **keywords):
    self.starttime = time.time()
    self.server = server

    self.timestamp = server.Register(SensorValue('timestamp', 0))
    self.rate = self.Register(EnumProperty, 'rate', 10, [10, 25], persistent=True)
    self.period = 1.0/self.rate.value

    self.loopfreq = self.Register(LoopFreqValue, 'loopfreq', 0)
    self.alignmentQ = self.Register(QuaternionValue, 'alignmentQ', [2**.5/2, -2**.5/2, 0, 0], persistent=True)
    self.alignmentQ.last = False
    self.heading_off = self.Register(RangeProperty, 'heading_offset', 0, -180, 180, persistent=True)
    self.heading_off.last = 3000 # invalid

    self.alignmentCounter = self.Register(Property, 'alignmentCounter', 0)
    self.last_alignmentCounter = False

    self.uptime = self.Register(TimeValue, 'uptime')

    def RegisterCalibration(name, default):
      calibration = self.Register(CalibrationProperty, name, server, default)
      calibration.age = self.Register(AgeValue, name+'.calibration.age', persistent=True)
      calibration.locked = self.Register(BooleanProperty, name+'.calibration.locked', False, persistent=True)
      calibration.sigmapoints = self.Register(RoundedValue, name+'.calibration.sigmapoints', False)
      calibration.sigmapoints.client_can_set = True
      calibration.log = self.Register(Property, name+'.calibration.log', '')
      return calibration

    self.accel_calibration = RegisterCalibration('accel', [[0, 0, 0, 1], 1])
    self.compass_calibration = RegisterCalibration('compass', [[0, 0, 0, 30, 0], [1, 1], 0])
    
    self.imu_pipe, imu_pipe = NonBlockingPipe('imu_pipe')
    imu_cal_pipe, self.imu_cal_pipe = NonBlockingPipe('imu_cal_pipe')

    self.poller = select.poll()
    self.poller.register(self.imu_pipe, select.POLLIN)

    self.auto_cal = calibration_fit.IMUAutomaticCalibration()

    self.lasttimestamp = 0

    self.headingrate = self.heel = 0
    self.heading_lowpass_constant = self.Register(RangeProperty, 'heading_lowpass_constant', .1, .01, 1)
    self.headingrate_lowpass_constant = self.Register(RangeProperty, 'headingrate_lowpass_constant', .1, .01, 1)
    self.headingraterate_lowpass_constant = self.Register(RangeProperty, 'headingraterate_lowpass_constant', .1, .01, 1)

    sensornames = ['accel', 'gyro', 'compass', 'accel.residuals', 'pitch', 'roll']
    sensornames += ['pitchrate', 'rollrate', 'headingrate', 'headingraterate', 'heel']
    sensornames += ['headingrate_lowpass', 'headingraterate_lowpass']
    directional_sensornames = ['heading', 'heading_lowpass']
    sensornames += directional_sensornames
    
    self.SensorValues = {}
    for name in sensornames:
      self.SensorValues[name] = self.Register(SensorValue, name, directional = name in directional_sensornames)

    # quaternion needs to report many more decimal places than other sensors
    sensornames += ['fusionQPose']
    self.SensorValues['fusionQPose'] = self.Register(SensorValue, 'fusionQPose', fmt='%.7f')
    
    sensornames += ['gyrobias']
    self.SensorValues['gyrobias'] = self.Register(SensorValue, 'gyrobias', persistent=True)

    self.imu_process = multiprocessing.Process(target=imu_process, args=(imu_pipe,imu_cal_pipe, self.accel_calibration.value[0], self.compass_calibration.value[0], self.SensorValues['gyrobias'].value, self.period))
    self.imu_process.start()

    self.last_imuread = time.time()

  def __del__(self):
    print('terminate imu process')
    self.imu_process.terminate()

  def Register(self, _type, name, *args, **kwargs):
    value = _type(*(['imu.' + name] + list(args)), **kwargs)
    return self.server.Register(value)
      
  def update_alignment(self, q):
    a2 = 2*math.atan2(q[3], q[0])
    heading_offset = a2*180/math.pi
    off = self.heading_off.value - heading_offset
    o = quaternion.angvec2quat(off*math.pi/180, [0, 0, 1])
    self.alignmentQ.update(quaternion.normalize(quaternion.multiply(q, o)))

  def IMURead(self):    
    data = False

    while self.poller.poll(0): # read all the data from the pipe
      data = self.imu_pipe.recv()

    if not data:
      if time.time() - self.last_imuread > 1 and self.loopfreq.value:
        print('IMURead failed!')
        self.loopfreq.set(0)
        for name in self.SensorValues:
          self.SensorValues[name].set(False)
        self.uptime.reset()
      return False
  
    if vector.norm(data['accel']) == 0:
      print('accel values invalid', data['accel'])
      return False

    t = time.time()
    self.timestamp.set(t-self.starttime)

    self.last_imuread = t
    self.loopfreq.strobe()

    # apply alignment calibration
    gyro_q = quaternion.rotvecquat(data['gyro'], data['fusionQPose'])

    data['pitchrate'], data['rollrate'], data['headingrate'] = map(math.degrees, gyro_q)
    origfusionQPose = data['fusionQPose']
    
    aligned = quaternion.multiply(data['fusionQPose'], self.alignmentQ.value)
    data['fusionQPose'] = quaternion.normalize(aligned) # floating point precision errors

    data['roll'], data['pitch'], data['heading'] = map(math.degrees, quaternion.toeuler(data['fusionQPose']))

    if data['heading'] < 0:
      data['heading'] += 360

    dt = data['timestamp'] - self.lasttimestamp
    self.lasttimestamp = data['timestamp']
    if dt > .02 and dt < .5:
      data['headingraterate'] = (data['headingrate'] - self.headingrate) / dt
    else:
      data['headingraterate'] = 0

    self.headingrate = data['headingrate']

    data['heel'] = self.heel = data['roll']*.03 + self.heel*.97
    #data['roll'] -= data['heel']

    data['gyro'] = list(map(math.degrees, data['gyro']))
    data['gyrobias'] = list(map(math.degrees, data['gyrobias']))

    # lowpass heading and rate
    llp = self.heading_lowpass_constant.value
    data['heading_lowpass'] = heading_filter(llp, data['heading'], self.SensorValues['heading_lowpass'].value)

    llp = self.headingrate_lowpass_constant.value
    data['headingrate_lowpass'] = llp*data['headingrate'] + (1-llp)*self.SensorValues['headingrate_lowpass'].value

    llp = self.headingraterate_lowpass_constant.value
    data['headingraterate_lowpass'] = llp*data['headingraterate'] + (1-llp)*self.SensorValues['headingraterate_lowpass'].value

    # set sensors
    for name in self.SensorValues:
      self.SensorValues[name].set(data[name])

    self.uptime.update()

    # count down to alignment
    if self.alignmentCounter.value != self.last_alignmentCounter:
      self.alignmentPose = [0, 0, 0, 0]

    if self.alignmentCounter.value > 0:
      self.alignmentPose = list(map(lambda x, y : x + y, self.alignmentPose, data['fusionQPose']))
      self.alignmentCounter.set(self.alignmentCounter.value-1)

      if self.alignmentCounter.value == 0:
        self.alignmentPose = quaternion.normalize(self.alignmentPose)
        adown = quaternion.rotvecquat([0, 0, 1], quaternion.conjugate(self.alignmentPose))
        alignment = []
        alignment = quaternion.vec2vec2quat([0, 0, 1], adown)
        alignment = quaternion.multiply(self.alignmentQ.value, alignment)
        
        if len(alignment):
          self.update_alignment(alignment)

      self.last_alignmentCounter = self.alignmentCounter.value

    # if alignment or heading offset changed:
    if self.heading_off.value != self.heading_off.last or self.alignmentQ.value != self.alignmentQ.last:
      self.update_alignment(self.alignmentQ.value)
      self.heading_off.last = self.heading_off.value
      self.alignmentQ.last = self.alignmentQ.value

    cal_data = {}
    if not self.accel_calibration.locked.value:
      cal_data['accel'] = list(data['accel'])
    if not self.compass_calibration.locked.value:
      cal_data['compass'] = list(data['compass'])
      cal_data['down'] = quaternion.rotvecquat([0, 0, 1], quaternion.conjugate(origfusionQPose))

    if cal_data:
      self.auto_cal.cal_pipe.send(cal_data)

    self.accel_calibration.age.update()
    self.compass_calibration.age.update()
    return data

class BoatIMUServer():
  def __init__(self):
    # setup all processes to exit on any signal
    self.childpids = []
    def cleanup(signal_number, frame=None):
        print('got signal', signal_number, 'cleaning up')
        while self.childpids:
            pid = self.childpids.pop()
            os.kill(pid, signal.SIGTERM) # get backtrace
        sys.stdout.flush()
        if signal_number != 'atexit':
          raise KeyboardInterrupt # to get backtrace on all processes

    # unfortunately we occasionally get this signal,
    # some sort of timing issue where python doesn't realize the pipe
    # is broken yet, so doesn't raise an exception
    def printpipewarning(signal_number, frame):
        print('got SIGPIPE, ignoring')

    import signal
    for s in range(1, 16):
        if s == 13:
            signal.signal(s, printpipewarning)
        elif s != 9:
            signal.signal(s, cleanup)

    #  server = pypilotServer()
    self.server = pypilotPipeServer()
    self.boatimu = BoatIMU(self.server)

    self.childpids = [self.boatimu.imu_process.pid, self.boatimu.auto_cal.process.pid,
                      self.server.process.pid]
    signal.signal(signal.SIGCHLD, cleanup)
    import atexit
    atexit.register(lambda : cleanup('atexit'))
    
    self.t00 = time.time()

  def iteration(self):
    self.server.HandleRequests()
    self.data = self.boatimu.IMURead()

    while True:
      dt = self.boatimu.period - (time.time() - self.t00)
      if dt <= 0 or dt >= self.boatimu.period:
        break
      time.sleep(dt)
    self.t00 = time.time()

def main():
  boatimu = BoatIMUServer()
  quiet = '-q' in sys.argv

  while True:
    boatimu.iteration()
    data = boatimu.data
    if data and not quiet:
      def line(*args):
        for a in args:
          sys.stdout.write(str(a))
          sys.stdout.write(' ')
        sys.stdout.write('\r')
        sys.stdout.flush()
      line('pitch', data['pitch'], 'roll', data['roll'], 'heading', data['heading'])

if __name__ == '__main__':
    main()
