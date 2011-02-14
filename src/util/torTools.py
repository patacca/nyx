"""
Helper for working with an active tor process. This both provides a wrapper for
accessing TorCtl and notifications of state changes to subscribers. To quickly
fetch a TorCtl instance to experiment with use the following:

>>> import util.torTools
>>> conn = util.torTools.connect()
>>> conn.get_info("version")["version"]
'0.2.1.24'
"""

import os
import time
import socket
import thread
import threading

from TorCtl import TorCtl, TorUtil

from util import enum, log, procTools, sysTools, uiTools

# enums for tor's controller state:
# INIT - attached to a new controller or restart/sighup signal received
# CLOSED - control port closed
State = enum.Enum("INIT", "CLOSED")

# Addresses of the default directory authorities for tor version 0.2.3.0-alpha
# (this comes from the dirservers array in src/or/config.c).
DIR_SERVERS = [("86.59.21.38", "80"),         # tor26
               ("128.31.0.39", "9031"),       # moria1
               ("216.224.124.114", "9030"),   # ides
               ("212.112.245.170", "80"),     # gabelmoo
               ("194.109.206.212", "80"),     # dizum
               ("193.23.244.244", "80"),      # dannenberg
               ("208.83.223.34", "443"),      # urras
               ("213.115.239.118", "443"),    # maatuska
               ("82.94.251.203", "80")]       # Tonga

# message logged by default when a controller can't set an event type
DEFAULT_FAILED_EVENT_MSG = "Unsupported event type: %s"

# TODO: check version when reattaching to controller and if version changes, flush?
# Skips attempting to set events we've failed to set before. This avoids
# logging duplicate warnings but can be problematic if controllers belonging
# to multiple versions of tor are attached, making this unreflective of the
# controller's capabilites. However, this is a pretty bizarre edge case.
DROP_FAILED_EVENTS = True
FAILED_EVENTS = set()

CONTROLLER = None # singleton Controller instance

# Valid keys for the controller's getInfo cache. This includes static GETINFO
# options (unchangable, even with a SETCONF) and other useful stats
CACHE_ARGS = ("version", "config-file", "exit-policy/default", "fingerprint",
              "config/names", "info/names", "features/names", "events/names",
              "nsEntry", "descEntry", "address", "bwRate", "bwBurst",
              "bwObserved", "bwMeasured", "flags", "pid", "pathPrefix",
              "startTime", "authorities")

# Tor has a couple messages (in or/router.c) for when our ip address changes:
# "Our IP Address has changed from <previous> to <current>; rebuilding
#   descriptor (source: <source>)."
# "Guessed our IP address as <current> (source: <source>)."
# 
# It would probably be preferable to use the EXTERNAL_ADDRESS event, but I'm
# not quite sure why it's not provided by check_descriptor_ipaddress_changed
# so erring on the side of inclusiveness by using the notice event instead.
ADDR_CHANGED_MSG_PREFIX = ("Our IP Address has changed from", "Guessed our IP address as")

TOR_CTL_CLOSE_MSG = "Tor closed control connection. Exiting event thread."
UNKNOWN = "UNKNOWN" # value used by cached information if undefined
CONFIG = {"torrc.map": {},
          "features.pathPrefix": "",
          "log.torCtlPortClosed": log.NOTICE,
          "log.torGetInfo": log.DEBUG,
          "log.torGetInfoCache": None,
          "log.torGetConf": log.DEBUG,
          "log.torSetConf": log.INFO,
          "log.torPrefixPathInvalid": log.NOTICE,
          "log.bsdJailFound": log.INFO,
          "log.unknownBsdJailId": log.WARN}

# events used for controller functionality:
# NOTICE - used to detect when tor is shut down
# NEWDESC, NS, and NEWCONSENSUS - used for cache invalidation
REQ_EVENTS = {"NOTICE": "this will be unable to detect when tor is shut down",
              "NEWDESC": "information related to descriptors will grow stale",
              "NS": "information related to the consensus will grow stale",
              "NEWCONSENSUS": "information related to the consensus will grow stale"}

# provides int -> str mappings for torctl event runlevels
TORCTL_RUNLEVELS = dict([(val, key) for (key, val) in TorUtil.loglevels.items()])

# ip address ranges substituted by the 'private' keyword
PRIVATE_IP_RANGES = ("0.0.0.0/8", "169.254.0.0/16", "127.0.0.0/8", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12")

# This prevents controllers from spawning worker threads (and by extension
# notifying status listeners). This is important when shutting down to prevent
# rogue threads from being alive during shutdown.

NO_SPAWN = False

def loadConfig(config):
  config.update(CONFIG)

def getPid(controlPort=9051, pidFilePath=None):
  """
  Attempts to determine the process id for a running tor process, using the
  following:
  1. GETCONF PidFile
  2. "pgrep -x tor"
  3. "pidof tor"
  4. "netstat -npl | grep 127.0.0.1:%s" % <tor control port>
  5. "ps -o pid -C tor"
  6. "sockstat -4l -P tcp -p %i | grep tor" % <tor control port>
  
  If pidof or ps provide multiple tor instances then their results are
  discarded (since only netstat can differentiate using the control port). This
  provides None if either no running process exists or it can't be determined.
  
  Arguments:
    controlPort - control port of the tor process if multiple exist
    pidFilePath - path to the pid file generated by tor
  """
  
  # attempts to fetch via the PidFile, failing if:
  # - the option is unset
  # - unable to read the file (such as insufficient permissions)
  
  if pidFilePath:
    try:
      pidFile = open(pidFilePath, "r")
      pidEntry = pidFile.readline().strip()
      pidFile.close()
      
      if pidEntry.isdigit(): return pidEntry
    except: pass
  
  # attempts to resolve using pgrep, failing if:
  # - tor is running under a different name
  # - there are multiple instances of tor
  try:
    results = sysTools.call("pgrep -x tor")
    if len(results) == 1 and len(results[0].split()) == 1:
      pid = results[0].strip()
      if pid.isdigit(): return pid
  except IOError: pass
  
  # attempts to resolve using pidof, failing if:
  # - tor's running under a different name
  # - there's multiple instances of tor
  try:
    results = sysTools.call("pidof tor")
    if len(results) == 1 and len(results[0].split()) == 1:
      pid = results[0].strip()
      if pid.isdigit(): return pid
  except IOError: pass
  
  # attempts to resolve using netstat, failing if:
  # - tor's being run as a different user due to permissions
  try:
    results = sysTools.call("netstat -npl | grep 127.0.0.1:%i" % controlPort)
    
    if len(results) == 1:
      results = results[0].split()[6] # process field (ex. "7184/tor")
      pid = results[:results.find("/")]
      if pid.isdigit(): return pid
  except IOError: pass
  
  # attempts to resolve using ps, failing if:
  # - tor's running under a different name
  # - there's multiple instances of tor
  try:
    results = sysTools.call("ps -o pid -C tor")
    if len(results) == 2:
      pid = results[1].strip()
      if pid.isdigit(): return pid
  except IOError: pass
  
  # attempts to resolve using sockstat, failing if:
  # - sockstat doesn't accept the -4 flag (BSD only)
  # - tor is running under a different name
  # - there are multiple instances of Tor, using the
  #   same control port on different addresses.
  # 
  # TODO: the later two issues could be solved by filtering for the control
  # port IP address instead of the process name.
  try:
    results = sysTools.call("sockstat -4l -P tcp -p %i | grep tor" % controlPort)
    if len(results) == 1 and len(results[0].split()) == 7:
      pid = results[0].split()[2]
      if pid.isdigit(): return pid
  except IOError: pass
  
  return None

def getBsdJailId():
  """
  Get the FreeBSD jail id for the monitored Tor process.
  """
  
  # Output when called from a FreeBSD jail or when Tor isn't jailed:
  #   JID
  #    0
  # 
  # Otherwise it's something like:
  #   JID
  #    1
  
  torPid = getConn().getMyPid()
  psOutput = sysTools.call("ps -p %s -o jid" % torPid)
  
  if len(psOutput) == 2 and len(psOutput[1].split()) == 1:
    jid = psOutput[1].strip()
    if jid.isdigit(): return int(jid)
  
  log.log(CONFIG["log.unknownBsdJailId"], "Failed to figure out the FreeBSD jail id. Assuming 0.")
  return 0

def getConn():
  """
  Singleton constructor for a Controller. Be aware that this starts as being
  uninitialized, needing a TorCtl instance before it's fully functional.
  """
  
  global CONTROLLER
  if CONTROLLER == None: CONTROLLER = Controller()
  return CONTROLLER

class Controller(TorCtl.PostEventListener):
  """
  TorCtl wrapper providing convenience functions, listener functionality for
  tor's state, and the capability for controller connections to be restarted
  if closed.
  """
  
  def __init__(self):
    TorCtl.PostEventListener.__init__(self)
    self.conn = None                    # None if uninitialized or controller's been closed
    self.connLock = threading.RLock()
    self.eventListeners = []            # instances listening for tor controller events
    self.torctlListeners = []           # callback functions for TorCtl events
    self.statusListeners = []           # callback functions for tor's state changes
    self.controllerEvents = []          # list of successfully set controller events
    self._fingerprintMappings = None    # mappings of ip -> [(port, fingerprint), ...]
    self._fingerprintLookupCache = {}   # lookup cache with (ip, port) -> fingerprint mappings
    self._fingerprintsAttachedCache = None # cache of relays we're connected to
    self._nicknameLookupCache = {}      # lookup cache with fingerprint -> nickname mappings
    self._isReset = False               # internal flag for tracking resets
    self._status = State.CLOSED         # current status of the attached control port
    self._statusTime = 0                # unix time-stamp for the duration of the status
    self.lastHeartbeat = 0              # time of the last tor event
    
    self._exitPolicyChecker = None
    self._exitPolicyLookupCache = {}    # mappings of ip/port tuples to if they were accepted by the policy or not
    
    # Logs issues and notices when fetching the path prefix if true. This is
    # only done once for the duration of the application to avoid pointless
    # messages.
    self._pathPrefixLogging = True
    
    # cached GETINFO parameters (None if unset or possibly changed)
    self._cachedParam = dict([(arg, "") for arg in CACHE_ARGS])
    
    # cached GETCONF parameters, entries consisting of:
    # (option, fetch_type) => value
    self._cachedConf = {}
    
    # directs TorCtl to notify us of events
    TorUtil.logger = self
    TorUtil.loglevel = "DEBUG"
  
  def init(self, conn=None):
    """
    Uses the given TorCtl instance for future operations, notifying listeners
    about the change.
    
    Arguments:
      conn - TorCtl instance to be used, if None then a new instance is fetched
             via the connect function
    """
    
    if conn == None:
      conn = TorCtl.connect()
      
      if conn == None: raise ValueError("Unable to initialize TorCtl instance.")
    
    if conn.is_live() and conn != self.conn:
      self.connLock.acquire()
      
      if self.conn: self.close() # shut down current connection
      self.conn = conn
      self.conn.add_event_listener(self)
      for listener in self.eventListeners: self.conn.add_event_listener(listener)
      
      # reset caches for ip -> fingerprint lookups
      self._fingerprintMappings = None
      self._fingerprintLookupCache = {}
      self._fingerprintsAttachedCache = None
      self._nicknameLookupCache = {}
      
      self._exitPolicyChecker = self.getExitPolicy()
      self._exitPolicyLookupCache = {}
      
      # sets the events listened for by the new controller (incompatible events
      # are dropped with a logged warning)
      self.setControllerEvents(self.controllerEvents)
      
      self.connLock.release()
      
      self._status = State.INIT
      self._statusTime = time.time()
      
      # notifies listeners that a new controller is available
      if not NO_SPAWN:
        thread.start_new_thread(self._notifyStatusListeners, (State.INIT,))
  
  def close(self):
    """
    Closes the current TorCtl instance and notifies listeners.
    """
    
    self.connLock.acquire()
    if self.conn:
      self.conn.close()
      
      # If we're closing due to an event from TorCtl (for instance, tor was
      # stopped) then TorCtl is shutting itself down and there's no need to
      # join on its thread (actually, this *is* the TorCtl thread in that
      # case so joining on it causes deadlock).
      # 
      # This poses a slight possability of shutting down with a live orphaned
      # thread if Tor is shut down, then arm shuts down before TorCtl has a
      # chance to terminate. However, I've never seen that occure so leaving
      # that alone for now.
      
      if not threading.currentThread() == self.conn._thread:
        self.conn._thread.join()
      
      self.conn = None
      self.connLock.release()
      
      self._status = State.CLOSED
      self._statusTime = time.time()
      
      # notifies listeners that the controller's been shut down
      if not NO_SPAWN:
        thread.start_new_thread(self._notifyStatusListeners, (State.CLOSED,))
    else: self.connLock.release()
  
  def isAlive(self):
    """
    Returns True if this has been initialized with a working TorCtl instance,
    False otherwise.
    """
    
    self.connLock.acquire()
    
    result = False
    if self.conn:
      if self.conn.is_live(): result = True
      else: self.close()
    
    self.connLock.release()
    return result
  
  def getHeartbeat(self):
    """
    Provides the time of the last registered tor event (if listening for BW
    events then this should occure every second if relay's still responsive).
    This returns zero if this has never received an event.
    """
    
    return self.lastHeartbeat
  
  def getTorCtl(self):
    """
    Provides the current TorCtl connection. If unset or closed then this
    returns None.
    """
    
    self.connLock.acquire()
    result = None
    if self.isAlive(): result = self.conn
    self.connLock.release()
    
    return result
  
  def getInfo(self, param, default = None, suppressExc = True):
    """
    Queries the control port for the given GETINFO option, providing the
    default if the response is undefined or fails for any reason (error
    response, control port closed, initiated, etc).
    
    Arguments:
      param       - GETINFO option to be queried
      default     - result if the query fails and exception's suppressed
      suppressExc - suppresses lookup errors (returning the default) if true,
                    otherwise this raises the original exception
    """
    
    self.connLock.acquire()
    
    startTime = time.time()
    result, raisedExc, isFromCache = default, None, False
    if self.isAlive():
      if param in CACHE_ARGS and self._cachedParam[param]:
        result = self._cachedParam[param]
        isFromCache = True
      else:
        try:
          getInfoVal = self.conn.get_info(param)[param]
          if getInfoVal != None: result = getInfoVal
        except (socket.error, TorCtl.ErrorReply, TorCtl.TorCtlClosed), exc:
          if type(exc) == TorCtl.TorCtlClosed: self.close()
          raisedExc = exc
    
    if not isFromCache and result and param in CACHE_ARGS:
      self._cachedParam[param] = result
    
    if isFromCache:
      msg = "GETINFO %s (cache fetch)" % param
      log.log(CONFIG["log.torGetInfoCache"], msg)
    else:
      msg = "GETINFO %s (runtime: %0.4f)" % (param, time.time() - startTime)
      log.log(CONFIG["log.torGetInfo"], msg)
    
    self.connLock.release()
    
    if not suppressExc and raisedExc: raise raisedExc
    else: return result
  
  def getOption(self, param, default = None, multiple = False, suppressExc = True):
    """
    Queries the control port for the given configuration option, providing the
    default if the response is undefined or fails for any reason. If multiple
    values exist then this arbitrarily returns the first unless the multiple
    flag is set.
    
    Arguments:
      param       - configuration option to be queried
      default     - result if the query fails and exception's suppressed
      multiple    - provides a list with all returned values if true, otherwise
                    this just provides the first result
      suppressExc - suppresses lookup errors (returning the default) if true,
                    otherwise this raises the original exception
    """
    
    fetchType = "list" if multiple else "str"
    
    if param in CONFIG["torrc.map"]:
      # This is among the options fetched via a special command. The results
      # are a set of values that (hopefully) contain the one we were
      # requesting.
      configMappings = self._getOption(CONFIG["torrc.map"][param], default, "map", suppressExc)
      if param in configMappings:
        if fetchType == "list": return configMappings[param]
        else: return configMappings[param][0]
      else: return default
    else:
      return self._getOption(param, default, fetchType, suppressExc)
  
  def getOptionMap(self, param, default = None, suppressExc = True):
    """
    Queries the control port for the given configuration option, providing back
    a mapping of config options to a list of the values returned.
    
    There's three use cases for GETCONF:
    - a single value is provided
    - multiple values are provided for the option queried
    - a set of options that weren't necessarily requested are returned (for
      instance querying HiddenServiceOptions gives HiddenServiceDir,
      HiddenServicePort, etc)
    
    The vast majority of the options fall into the first two catagories, in
    which case calling getOption is sufficient. However, for the special
    options that give a set of values this provides back the full response. As
    of tor version 0.2.1.25 HiddenServiceOptions was the only option like this.
    
    The getOption function accounts for these special mappings, and the only
    advantage to this funtion is that it provides all related values in a
    single response.
    
    Arguments:
      param       - configuration option to be queried
      default     - result if the query fails and exception's suppressed
      suppressExc - suppresses lookup errors (returning the default) if true,
                    otherwise this raises the original exception
    """
    
    return self._getOption(param, default, "map", suppressExc)
  
  # TODO: cache isn't updated (or invalidated) during SETCONF events:
  # https://trac.torproject.org/projects/tor/ticket/1692
  def _getOption(self, param, default, fetchType, suppressExc):
    if not fetchType in ("str", "list", "map"):
      msg = "BUG: unrecognized fetchType in torTools._getOption (%s)" % fetchType
      log.log(log.ERR, msg)
      return default
    
    self.connLock.acquire()
    startTime, raisedExc, isFromCache = time.time(), None, False
    result = {} if fetchType == "map" else []
    
    if self.isAlive():
      if (param.lower(), fetchType) in self._cachedConf:
        isFromCache = True
        result = self._cachedConf[(param.lower(), fetchType)]
      else:
        try:
          if fetchType == "str":
            getConfVal = self.conn.get_option(param)[0][1]
            if getConfVal != None: result = getConfVal
          else:
            for key, value in self.conn.get_option(param):
              if value != None:
                if fetchType == "list": result.append(value)
                elif fetchType == "map":
                  if key in result: result[key].append(value)
                  else: result[key] = [value]
        except (socket.error, TorCtl.ErrorReply, TorCtl.TorCtlClosed), exc:
          if type(exc) == TorCtl.TorCtlClosed: self.close()
          result, raisedExc = default, exc
    
    if not isFromCache and result:
      cacheValue = result
      if fetchType == "list": cacheValue = list(result)
      elif fetchType == "map": cacheValue = dict(result)
      self._cachedConf[(param.lower(), fetchType)] = cacheValue
    
    runtimeLabel = "cache fetch" if isFromCache else "runtime: %0.4f" % (time.time() - startTime)
    msg = "GETCONF %s (%s)" % (param, runtimeLabel)
    log.log(CONFIG["log.torGetConf"], msg)
    
    self.connLock.release()
    
    if not suppressExc and raisedExc: raise raisedExc
    elif result == []: return default
    else: return result
  
  def setOption(self, param, value):
    """
    Issues a SETCONF to set the given option/value pair. An exeptions raised
    if it fails to be set.
    
    Arguments:
      param - configuration option to be set
      value - value to set the parameter to (this can be either a string or a
              list of strings)
    """
    
    isMultiple = isinstance(value, list) or isinstance(value, tuple)
    self.connLock.acquire()
    
    startTime, raisedExc = time.time(), None
    if self.isAlive():
      try:
        if isMultiple: self.conn.set_options([(param, val) for val in value])
        else: self.conn.set_option(param, value)
        
        # flushing cached values (needed until we can detect SETCONF calls)
        for fetchType in ("str", "list", "map"):
          entry = (param.lower(), fetchType)
          
          if entry in self._cachedConf:
            del self._cachedConf[entry]
        
        # special caches for the exit policy
        if param.lower() == "exitpolicy":
          self._exitPolicyChecker = self.getExitPolicy()
          self._exitPolicyLookupCache = {}
      except (socket.error, TorCtl.ErrorReply, TorCtl.TorCtlClosed), exc:
        if type(exc) == TorCtl.TorCtlClosed: self.close()
        elif type(exc) == TorCtl.ErrorReply:
          excStr = str(exc)
          if excStr.startswith("513 Unacceptable option value: "):
            # crops off the common error prefix
            excStr = excStr[31:]
            
            # Truncates messages like:
            # Value 'BandwidthRate la de da' is malformed or out of bounds.
            # to: Value 'la de da' is malformed or out of bounds.
            if excStr.startswith("Value '"):
              excStr = excStr.replace("%s " % param, "", 1)
            
            exc = TorCtl.ErrorReply(excStr)
        
        raisedExc = exc
    
    self.connLock.release()
    
    setCall = "%s %s" % (param, ", ".join(value) if isMultiple else value)
    excLabel = "failed: \"%s\", " % raisedExc if raisedExc else ""
    msg = "SETCONF %s (%sruntime: %0.4f)" % (setCall.strip(), excLabel, time.time() - startTime)
    log.log(CONFIG["log.torSetConf"], msg)
    
    if raisedExc: raise raisedExc
  
  def getMyNetworkStatus(self, default = None):
    """
    Provides the network status entry for this relay if available. This is
    occasionally expanded so results may vary depending on tor's version. For
    0.2.2.13 they contained entries like the following:
    
    r caerSidi p1aag7VwarGxqctS7/fS0y5FU+s 9On1TRGCEpljszPpJR1hKqlzaY8 2010-05-26 09:26:06 76.104.132.98 9001 0
    s Fast HSDir Named Running Stable Valid
    w Bandwidth=25300
    p reject 1-65535
    
    Arguments:
      default - result if the query fails
    """
    
    return self._getRelayAttr("nsEntry", default)
  
  def getMyDescriptor(self, default = None):
    """
    Provides the descriptor entry for this relay if available.
    
    Arguments:
      default - result if the query fails
    """
    
    return self._getRelayAttr("descEntry", default)
  
  def getMyBandwidthRate(self, default = None):
    """
    Provides the effective relaying bandwidth rate of this relay. Currently
    this doesn't account for SETCONF events.
    
    Arguments:
      default - result if the query fails
    """
    
    return self._getRelayAttr("bwRate", default)
  
  def getMyBandwidthBurst(self, default = None):
    """
    Provides the effective bandwidth burst rate of this relay. Currently this
    doesn't account for SETCONF events.
    
    Arguments:
      default - result if the query fails
    """
    
    return self._getRelayAttr("bwBurst", default)
  
  def getMyBandwidthObserved(self, default = None):
    """
    Provides the relay's current observed bandwidth (the throughput determined
    from historical measurements on the client side). This is used in the
    heuristic used for path selection if the measured bandwidth is undefined.
    This is fetched from the descriptors and hence will get stale if
    descriptors aren't periodically updated.
    
    Arguments:
      default - result if the query fails
    """
    
    return self._getRelayAttr("bwObserved", default)
  
  def getMyBandwidthMeasured(self, default = None):
    """
    Provides the relay's current measured bandwidth (the throughput as noted by
    the directory authorities and used by clients for relay selection). This is
    undefined if not in the consensus or with older versions of Tor. Depending
    on the circumstances this can be from a variety of things (observed,
    measured, weighted measured, etc) as described by:
    https://trac.torproject.org/projects/tor/ticket/1566
    
    Arguments:
      default - result if the query fails
    """
    
    return self._getRelayAttr("bwMeasured", default)
  
  def getMyFlags(self, default = None):
    """
    Provides the flags held by this relay.
    
    Arguments:
      default - result if the query fails or this relay isn't a part of the consensus yet
    """
    
    return self._getRelayAttr("flags", default)
  
  def getMyPid(self):
    """
    Provides the pid of the attached tor process (None if no controller exists
    or this can't be determined).
    """
    
    return self._getRelayAttr("pid", None)
  
  def getMyDirAuthorities(self):
    """
    Provides a listing of IP/port tuples for the directory authorities we've
    been configured to use. If set in the configuration then these are custom
    authorities, otherwise its an estimate of what Tor has been hardcoded to
    use (unfortunately, this might be out of date).
    """
    
    return self._getRelayAttr("authorities", [])
  
  def getPathPrefix(self):
    """
    Provides the path prefix that should be used for fetching tor resources.
    If undefined and Tor is inside a jail under FreeBsd then this provides the
    jail's path.
    """
    
    result = self._getRelayAttr("pathPrefix", "")
    
    if result == UNKNOWN: return ""
    else: return result
  
  def getStartTime(self):
    """
    Provides the unix time for when the tor process first started. If this
    can't be determined then this provides None.
    """
    
    result = self._getRelayAttr("startTime", None)
    
    if result == UNKNOWN: return None
    else: return result
  
  def getStatus(self):
    """
    Provides a tuple consisting of the control port's current status and unix
    time-stamp for when it became this way (zero if no status has yet to be
    set).
    """
    
    return (self._status, self._statusTime)
  
  def isExitingAllowed(self, ipAddress, port):
    """
    Checks if the given destination can be exited to by this relay, returning
    True if so and False otherwise.
    """
    
    self.connLock.acquire()
    
    result = False
    if self.isAlive():
      # query the policy if it isn't yet cached
      if not (ipAddress, port) in self._exitPolicyLookupCache:
        isAccepted = self._exitPolicyChecker.check(ipAddress, port)
        self._exitPolicyLookupCache[(ipAddress, port)] = isAccepted
      
      result = self._exitPolicyLookupCache[(ipAddress, port)]
    
    self.connLock.release()
    
    return result
  
  def getExitPolicy(self):
    """
    Provides an ExitPolicy instance for the head of this relay's exit policy
    chain. If there's no active connection then this provides None.
    """
    
    self.connLock.acquire()
    
    result = None
    if self.isAlive():
      policyEntries = []
      for exitPolicy in self.getOption("ExitPolicy", [], True):
        policyEntries += [policy.strip() for policy in exitPolicy.split(",")]
      
      # appends the default exit policy
      defaultExitPolicy = self.getInfo("exit-policy/default")
      
      if defaultExitPolicy:
        policyEntries += defaultExitPolicy.split(",")
      
      # construct the policy chain backwards
      policyEntries.reverse()
      
      for entry in policyEntries:
        result = ExitPolicy(entry, result)
      
      # Checks if we are rejecting private connections. If set, this appends
      # 'reject private' and 'reject <my ip>' to the start of our policy chain.
      isPrivateRejected = self.getOption("ExitPolicyRejectPrivate", True)
      
      if isPrivateRejected:
        result = ExitPolicy("reject private", result)
        
        myAddress = self.getInfo("address")
        if myAddress: result = ExitPolicy("reject %s" % myAddress, result)
    
    self.connLock.release()
    
    return result
  
  def getRelayFingerprint(self, relayAddress, relayPort = None):
    """
    Provides the fingerprint associated with the given address. If there's
    multiple potential matches or the mapping is unknown then this returns
    None. This disambiguates the fingerprint if there's multiple relays on
    the same ip address by several methods, one of them being to pick relays
    we have a connection with.
    
    Arguments:
      relayAddress - address of relay to be returned
      relayPort    - orport of relay (to further narrow the results)
    """
    
    self.connLock.acquire()
    
    result = None
    if self.isAlive():
      # query the fingerprint if it isn't yet cached
      if not (relayAddress, relayPort) in self._fingerprintLookupCache:
        relayFingerprint = self._getRelayFingerprint(relayAddress, relayPort)
        self._fingerprintLookupCache[(relayAddress, relayPort)] = relayFingerprint
      
      result = self._fingerprintLookupCache[(relayAddress, relayPort)]
    
    self.connLock.release()
    
    return result
  
  def getRelayNickname(self, relayFingerprint):
    """
    Provides the nickname associated with the given relay. This provides None
    if no such relay exists, and "Unnamed" if the name hasn't been set.
    
    Arguments:
      relayFingerprint - fingerprint of the relay
    """
    
    self.connLock.acquire()
    
    result = None
    if self.isAlive():
      # query the nickname if it isn't yet cached
      if not relayFingerprint in self._nicknameLookupCache:
        if relayFingerprint == getInfo("fingerprint"):
          # this is us, simply check the config
          myNickname = self.getOption("Nickname", "Unnamed")
          self._nicknameLookupCache[relayFingerprint] = myNickname
        else:
          # check the consensus for the relay
          nsEntry = self.getInfo("ns/id/%s" % relayFingerprint)
          
          if nsEntry: relayNickname = nsEntry[2:nsEntry.find(" ", 2)]
          else: relayNickname = None
          
          self._nicknameLookupCache[relayFingerprint] = relayNickname
      
      result = self._nicknameLookupCache[relayFingerprint]
    
    self.connLock.release()
    
    return result
  
  def addEventListener(self, listener):
    """
    Directs further tor controller events to callback functions of the
    listener. If a new control connection is initialized then this listener is
    reattached.
    
    Arguments:
      listener - TorCtl.PostEventListener instance listening for events
    """
    
    self.connLock.acquire()
    self.eventListeners.append(listener)
    if self.isAlive(): self.conn.add_event_listener(listener)
    self.connLock.release()
  
  def addTorCtlListener(self, callback):
    """
    Directs further TorCtl events to the callback function. Events are composed
    of a runlevel and message tuple.
    
    Arguments:
      callback - functor that'll accept the events, expected to be of the form:
                 myFunction(runlevel, msg)
    """
    
    self.torctlListeners.append(callback)
  
  def addStatusListener(self, callback):
    """
    Directs further events related to tor's controller status to the callback
    function.
    
    Arguments:
      callback - functor that'll accept the events, expected to be of the form:
                 myFunction(controller, eventType)
    """
    
    self.statusListeners.append(callback)
  
  def removeStatusListener(self, callback):
    """
    Stops listener from being notified of further events. This returns true if a
    listener's removed, false otherwise.
    
    Arguments:
      callback - functor to be removed
    """
    
    if callback in self.statusListeners:
      self.statusListeners.remove(callback)
      return True
    else: return False
  
  def getControllerEvents(self):
    """
    Provides the events the controller's currently configured to listen for.
    """
    
    return list(self.controllerEvents)
  
  def setControllerEvents(self, events):
    """
    Sets the events being requested from any attached tor instance, logging
    warnings for event types that aren't supported (possibly due to version
    issues). Events in REQ_EVENTS will also be included, logging at the error
    level with an additional description in case of failure.
    
    This remembers the successfully set events and tries to request them from
    any tor instance it attaches to in the future too (again logging and
    dropping unsuccessful event types).
    
    This returns the listing of event types that were successfully set. If not
    currently attached to a tor instance then all events are assumed to be ok,
    then attempted when next attached to a control port.
    
    Arguments:
      events - listing of events to be set
    """
    
    self.connLock.acquire()
    
    returnVal = []
    if self.isAlive():
      events = set(events)
      events = events.union(set(REQ_EVENTS.keys()))
      unavailableEvents = set()
      
      # removes anything we've already failed to set
      if DROP_FAILED_EVENTS:
        unavailableEvents.update(events.intersection(FAILED_EVENTS))
        events.difference_update(FAILED_EVENTS)
      
      # initial check for event availability, using the 'events/names' GETINFO
      # option to detect invalid events
      validEvents = self.getInfo("events/names")
      
      if validEvents:
        validEvents = set(validEvents.split())
        unavailableEvents.update(events.difference(validEvents))
        events.intersection_update(validEvents)
      
      # attempt to set events via trial and error
      isEventsSet, isAbandoned = False, False
      
      while not isEventsSet and not isAbandoned:
        try:
          self.conn.set_events(list(events))
          isEventsSet = True
        except TorCtl.ErrorReply, exc:
          msg = str(exc)
          
          if "Unrecognized event" in msg:
            # figure out type of event we failed to listen for
            start = msg.find("event \"") + 7
            end = msg.rfind("\"")
            failedType = msg[start:end]
            
            unavailableEvents.add(failedType)
            events.discard(failedType)
          else:
            # unexpected error, abandon attempt
            isAbandoned = True
        except TorCtl.TorCtlClosed:
          self.close()
          isAbandoned = True
      
      FAILED_EVENTS.update(unavailableEvents)
      if not isAbandoned:
        # logs warnings or errors for failed events
        for eventType in unavailableEvents:
          defaultMsg = DEFAULT_FAILED_EVENT_MSG % eventType
          if eventType in REQ_EVENTS:
            log.log(log.ERR, defaultMsg + " (%s)" % REQ_EVENTS[eventType])
          else:
            log.log(log.WARN, defaultMsg)
        
        self.controllerEvents = list(events)
        returnVal = list(events)
    else:
      # attempts to set the events when next attached to a control port
      self.controllerEvents = list(events)
      returnVal = list(events)
    
    self.connLock.release()
    return returnVal
  
  def reload(self, issueSighup = False):
    """
    This resets tor (sending a RELOAD signal to the control port) causing tor's
    internal state to be reset and the torrc reloaded. This can either be done
    by...
      - the controller via a RELOAD signal (default and suggested)
          conn.send_signal("RELOAD")
      - system reload signal (hup)
          pkill -sighup tor
    
    The later isn't really useful unless there's some reason the RELOAD signal
    won't do the trick. Both methods raise an IOError in case of failure.
    
    Arguments:
      issueSighup - issues a sighup rather than a controller RELOAD signal
    """
    
    self.connLock.acquire()
    
    raisedException = None
    if self.isAlive():
      if not issueSighup:
        try:
          self.conn.send_signal("RELOAD")
          self._cachedParam = dict([(arg, "") for arg in CACHE_ARGS])
          self._cachedConf = {}
        except Exception, exc:
          # new torrc parameters caused an error (tor's likely shut down)
          # BUG: this doesn't work - torrc errors still cause TorCtl to crash... :(
          # http://bugs.noreply.org/flyspray/index.php?do=details&id=1329
          raisedException = IOError(str(exc))
      else:
        try:
          # Redirects stderr to stdout so we can check error status (output
          # should be empty if successful). Example error:
          # pkill: 5592 - Operation not permitted
          #
          # note that this may provide multiple errors, even if successful,
          # hence this:
          #   - only provide an error if Tor fails to log a sighup
          #   - provide the error message associated with the tor pid (others
          #     would be a red herring)
          if not sysTools.isAvailable("pkill"):
            raise IOError("pkill command is unavailable")
          
          self._isReset = False
          pkillCall = os.popen("pkill -sighup ^tor$ 2> /dev/stdout")
          pkillOutput = pkillCall.readlines()
          pkillCall.close()
          
          # Give the sighupTracker a moment to detect the sighup signal. This
          # is, of course, a possible concurrency bug. However I'm not sure
          # of a better method for blocking on this...
          waitStart = time.time()
          while time.time() - waitStart < 1:
            time.sleep(0.1)
            if self._isReset: break
          
          if not self._isReset:
            errorLine, torPid = "", self.getMyPid()
            if torPid:
              for line in pkillOutput:
                if line.startswith("pkill: %s - " % torPid):
                  errorLine = line
                  break
            
            if errorLine: raise IOError(" ".join(errorLine.split()[3:]))
            else: raise IOError("failed silently")
          
          self._cachedParam = dict([(arg, "") for arg in CACHE_ARGS])
          self._cachedConf = {}
        except IOError, exc:
          raisedException = exc
    
    self.connLock.release()
    
    if raisedException: raise raisedException
  
  def msg_event(self, event):
    """
    Listens for reload signal (hup), which is either produced by:
    causing the torrc and internal state to be reset.
    """
    
    if event.level == "NOTICE" and event.msg.startswith("Received reload signal (hup)"):
      self._isReset = True
      
      self._status = State.INIT
      self._statusTime = time.time()
      
      if not NO_SPAWN:
        thread.start_new_thread(self._notifyStatusListeners, (State.INIT,))
  
  def ns_event(self, event):
    self._updateHeartbeat()
    
    myFingerprint = self.getInfo("fingerprint")
    if myFingerprint:
      for ns in event.nslist:
        if ns.idhex == myFingerprint:
          self._cachedParam["nsEntry"] = None
          self._cachedParam["flags"] = None
          self._cachedParam["bwMeasured"] = None
          return
    else:
      self._cachedParam["nsEntry"] = None
      self._cachedParam["flags"] = None
      self._cachedParam["bwMeasured"] = None
  
  def new_consensus_event(self, event):
    self._updateHeartbeat()
    
    self.connLock.acquire()
    
    self._cachedParam["nsEntry"] = None
    self._cachedParam["flags"] = None
    self._cachedParam["bwMeasured"] = None
    
    # reconstructs consensus based mappings
    self._fingerprintLookupCache = {}
    self._fingerprintsAttachedCache = None
    self._nicknameLookupCache = {}
    
    if self._fingerprintMappings != None:
      self._fingerprintMappings = self._getFingerprintMappings(event.nslist)
    
    self.connLock.release()
  
  def new_desc_event(self, event):
    self._updateHeartbeat()
    
    self.connLock.acquire()
    
    myFingerprint = self.getInfo("fingerprint")
    if not myFingerprint or myFingerprint in event.idlist:
      self._cachedParam["descEntry"] = None
      self._cachedParam["bwObserved"] = None
    
    # If we're tracking ip address -> fingerprint mappings then update with
    # the new relays.
    self._fingerprintLookupCache = {}
    self._fingerprintsAttachedCache = None
    
    if self._fingerprintMappings != None:
      for fingerprint in event.idlist:
        # gets consensus data for the new descriptor
        try: nsLookup = self.conn.get_network_status("id/%s" % fingerprint)
        except (socket.error, TorCtl.ErrorReply, TorCtl.TorCtlClosed): continue
        
        if len(nsLookup) > 1:
          # multiple records for fingerprint (shouldn't happen)
          log.log(log.WARN, "Multiple consensus entries for fingerprint: %s" % fingerprint)
          continue
        
        # updates fingerprintMappings with new data
        newRelay = nsLookup[0]
        if newRelay.ip in self._fingerprintMappings:
          # if entry already exists with the same orport, remove it
          orportMatch = None
          for entryPort, entryFingerprint in self._fingerprintMappings[newRelay.ip]:
            if entryPort == newRelay.orport:
              orportMatch = (entryPort, entryFingerprint)
              break
          
          if orportMatch: self._fingerprintMappings[newRelay.ip].remove(orportMatch)
          
          # add the new entry
          self._fingerprintMappings[newRelay.ip].append((newRelay.orport, newRelay.idhex))
        else:
          self._fingerprintMappings[newRelay.ip] = [(newRelay.orport, newRelay.idhex)]
    
    self.connLock.release()
  
  def circ_status_event(self, event):
    self._updateHeartbeat()
    
    # CIRC events aren't required, but if one's received then flush this cache
    # since it uses circuit-status results.
    self._fingerprintsAttachedCache = None
  
  def buildtimeout_set_event(self, event):
    self._updateHeartbeat()
  
  def stream_status_event(self, event):
    self._updateHeartbeat()
  
  def or_conn_status_event(self, event):
    self._updateHeartbeat()
  
  def stream_bw_event(self, event):
    self._updateHeartbeat()
  
  def bandwidth_event(self, event):
    self._updateHeartbeat()
  
  def address_mapped_event(self, event):
    self._updateHeartbeat()
  
  def unknown_event(self, event):
    self._updateHeartbeat()
  
  def log(self, level, msg, *args):
    """
    Tracks TorCtl events. Ugly hack since TorCtl/TorUtil.py expects a
    logging.Logger instance.
    """
    
    # notifies listeners of TorCtl events
    for callback in self.torctlListeners: callback(TORCTL_RUNLEVELS[level], msg)
    
    # checks if TorCtl is providing a notice that control port is closed
    if TOR_CTL_CLOSE_MSG in msg: self.close()
    
    # if the message is informing us of our ip address changing then clear
    # its cached value
    isAddrChangeEvent = False
    for prefix in ADDR_CHANGED_MSG_PREFIX:
      isAddrChangeEvent |= msg.startswith(prefix)
    
    if isAddrChangeEvent and "address" in self._cachedParam:
      del self._cachedParam["address"]
  
  def _updateHeartbeat(self):
    """
    Called on any event occurance to note the time it occured.
    """
    
    # alternative is to use the event's timestamp (via event.arrived_at)
    self.lastHeartbeat = time.time()
  
  def _getFingerprintMappings(self, nsList = None):
    """
    Provides IP address to (port, fingerprint) tuple mappings for all of the
    currently cached relays.
    
    Arguments:
      nsList - network status listing (fetched if not provided)
    """
    
    results = {}
    if self.isAlive():
      # fetch the current network status if not provided
      if not nsList:
        try: nsList = self.conn.get_network_status()
        except (socket.error, TorCtl.TorCtlClosed, TorCtl.ErrorReply): nsList = []
      
      # construct mappings of ips to relay data
      for relay in nsList:
        if relay.ip in results: results[relay.ip].append((relay.orport, relay.idhex))
        else: results[relay.ip] = [(relay.orport, relay.idhex)]
    
    return results
  
  def _getRelayFingerprint(self, relayAddress, relayPort):
    """
    Provides the fingerprint associated with the address/port combination.
    
    Arguments:
      relayAddress - address of relay to be returned
      relayPort    - orport of relay (to further narrow the results)
    """
    
    # If we were provided with a string port then convert to an int (so
    # lookups won't mismatch based on type).
    if isinstance(relayPort, str): relayPort = int(relayPort)
    
    # checks if this matches us
    if relayAddress == self.getInfo("address"):
      if not relayPort or relayPort == self.getOption("ORPort"):
        return self.getInfo("fingerprint")
    
    # if we haven't yet populated the ip -> fingerprint mappings then do so
    if self._fingerprintMappings == None:
      self._fingerprintMappings = self._getFingerprintMappings()
    
    potentialMatches = self._fingerprintMappings.get(relayAddress)
    if not potentialMatches: return None # no relay matches this ip address
    
    if len(potentialMatches) == 1:
      # There's only one relay belonging to this ip address. If the port
      # matches then we're done.
      match = potentialMatches[0]
      
      if relayPort and match[0] != relayPort: return None
      else: return match[1]
    elif relayPort:
      # Multiple potential matches, so trying to match based on the port.
      for entryPort, entryFingerprint in potentialMatches:
        if entryPort == relayPort:
          return entryFingerprint
    
    # Disambiguates based on our orconn-status and circuit-status results.
    # This only includes relays we're connected to, so chances are pretty
    # slim that we'll still have a problem narrowing this down. Note that we
    # aren't necessarily checking for events that can create new client
    # circuits (so this cache might be a little dirty).
    
    # populates the cache
    if self._fingerprintsAttachedCache == None:
      self._fingerprintsAttachedCache = []
      
      # orconn-status has entries of the form:
      # $33173252B70A50FE3928C7453077936D71E45C52=shiven CONNECTED
      orconnResults = self.getInfo("orconn-status")
      if orconnResults:
        for line in orconnResults.split("\n"):
          self._fingerprintsAttachedCache.append(line[1:line.find("=")])
      
      # circuit-status has entries of the form:
      # 7 BUILT $33173252B70A50FE3928C7453077936D71E45C52=shiven,...
      circStatusResults = self.getInfo("circuit-status")
      if circStatusResults:
        for line in circStatusResults.split("\n"):
          clientEntries = line.split(" ")[2].split(",")
          
          for entry in clientEntries:
            self._fingerprintsAttachedCache.append(entry[1:entry.find("=")])
    
    # narrow to only relays we have a connection to
    attachedMatches = []
    for _, entryFingerprint in potentialMatches:
      if entryFingerprint in self._fingerprintsAttachedCache:
        attachedMatches.append(entryFingerprint)
    
    if len(attachedMatches) == 1:
      return attachedMatches[0]
    
    # Highly unlikely, but still haven't found it. Last we'll use some
    # tricks from Mike's ConsensusTracker, excluding possiblities that
    # have...
    # - lost their Running flag
    # - list a bandwidth of 0
    # - have 'opt hibernating' set
    # 
    # This involves constructing a TorCtl Router and checking its 'down'
    # flag (which is set by the three conditions above). This is the last
    # resort since it involves a couple GETINFO queries.
    
    for entryPort, entryFingerprint in list(potentialMatches):
      try:
        nsCall = self.conn.get_network_status("id/%s" % entryFingerprint)
        if not nsCall: raise TorCtl.ErrorReply() # network consensus couldn't be fetched
        nsEntry = nsCall[0]
        
        descEntry = self.getInfo("desc/id/%s" % entryFingerprint)
        if not descEntry: raise TorCtl.ErrorReply() # relay descriptor couldn't be fetched
        descLines = descEntry.split("\n")
        
        isDown = TorCtl.Router.build_from_desc(descLines, nsEntry).down
        if isDown: potentialMatches.remove((entryPort, entryFingerprint))
      except (socket.error, TorCtl.ErrorReply, TorCtl.TorCtlClosed): pass
    
    if len(potentialMatches) == 1:
      return potentialMatches[0][1]
    else: return None
  
  def _getRelayAttr(self, key, default, cacheUndefined = True):
    """
    Provides information associated with this relay, using the cached value if
    available and otherwise looking it up.
    
    Arguments:
      key            - parameter being queried (from CACHE_ARGS)
      default        - value to be returned if undefined
      cacheUndefined - caches when values are undefined, avoiding further
                       lookups if true
    """
    
    currentVal = self._cachedParam[key]
    if currentVal:
      if currentVal == UNKNOWN: return default
      else: return currentVal
    
    self.connLock.acquire()
    
    currentVal, result = self._cachedParam[key], None
    if not currentVal and self.isAlive():
      # still unset - fetch value
      if key in ("nsEntry", "descEntry"):
        myFingerprint = self.getInfo("fingerprint")
        
        if myFingerprint:
          queryType = "ns" if key == "nsEntry" else "desc"
          queryResult = self.getInfo("%s/id/%s" % (queryType, myFingerprint))
          if queryResult: result = queryResult.split("\n")
      elif key == "bwRate":
        # effective relayed bandwidth is the minimum of BandwidthRate,
        # MaxAdvertisedBandwidth, and RelayBandwidthRate (if set)
        effectiveRate = int(self.getOption("BandwidthRate"))
        
        relayRate = self.getOption("RelayBandwidthRate")
        if relayRate and relayRate != "0":
          effectiveRate = min(effectiveRate, int(relayRate))
        
        maxAdvertised = self.getOption("MaxAdvertisedBandwidth")
        if maxAdvertised: effectiveRate = min(effectiveRate, int(maxAdvertised))
        
        result = effectiveRate
      elif key == "bwBurst":
        # effective burst (same for BandwidthBurst and RelayBandwidthBurst)
        effectiveBurst = int(self.getOption("BandwidthBurst"))
        
        relayBurst = self.getOption("RelayBandwidthBurst")
        if relayBurst and relayBurst != "0":
          effectiveBurst = min(effectiveBurst, int(relayBurst))
        
        result = effectiveBurst
      elif key == "bwObserved":
        for line in self.getMyDescriptor([]):
          if line.startswith("bandwidth"):
            # line should look something like:
            # bandwidth 40960 102400 47284
            comp = line.split()
            
            if len(comp) == 4 and comp[-1].isdigit():
              result = int(comp[-1])
              break
      elif key == "bwMeasured":
        # TODO: Currently there's no client side indication of what type of
        # measurement was used. Include this in results if it's ever available.
        
        for line in self.getMyNetworkStatus([]):
          if line.startswith("w Bandwidth="):
            bwValue = line[12:]
            if bwValue.isdigit(): result = int(bwValue)
            break
      elif key == "flags":
        for line in self.getMyNetworkStatus([]):
          if line.startswith("s "):
            result = line[2:].split()
            break
      elif key == "pid":
        result = getPid(int(self.getOption("ControlPort", 9051)), self.getOption("PidFile"))
      elif key == "pathPrefix":
        # make sure the path prefix is valid and exists (providing a notice if not)
        prefixPath = CONFIG["features.pathPrefix"].strip()
        
        # adjusts the prefix path to account for jails under FreeBSD (many
        # thanks to Fabian Keil!)
        if not prefixPath and os.uname()[0] == "FreeBSD":
          jid = getBsdJailId()
          if jid != 0:
            # Output should be something like:
            #    JID  IP Address      Hostname      Path
            #      1  10.0.0.2        tor-jail      /usr/jails/tor-jail
            jlsOutput = sysTools.call("jls -j %s" % jid)
            
            if len(jlsOutput) == 2 and len(jlsOutput[1].split()) == 4:
              prefixPath = jlsOutput[1].split()[3]
              
              if self._pathPrefixLogging:
                msg = "Adjusting paths to account for Tor running in a jail at: %s" % prefixPath
                log.log(CONFIG["log.bsdJailFound"], msg)
        
        if prefixPath:
          # strips off ending slash from the path
          if prefixPath.endswith("/"): prefixPath = prefixPath[:-1]
          
          # avoid using paths that don't exist
          if self._pathPrefixLogging and prefixPath and not os.path.exists(prefixPath):
            msg = "The prefix path set in your config (%s) doesn't exist." % prefixPath
            log.log(CONFIG["log.torPrefixPathInvalid"], msg)
            prefixPath = ""
        
        self._pathPrefixLogging = False # prevents logging if fetched again
        result = prefixPath
      elif key == "startTime":
        myPid = self.getMyPid()
        
        if myPid:
          try:
            if procTools.isProcAvailable():
              result = float(procTools.getStats(myPid, procTools.Stat.START_TIME)[0])
            else:
              psCall = sysTools.call("ps -p %s -o etime" % myPid)
              
              if psCall and len(psCall) >= 2:
                etimeEntry = psCall[1].strip()
                result = time.time() - uiTools.parseShortTimeLabel(etimeEntry)
          except: pass
      elif key == "authorities":
        # There's two configuration options that can overwrite the default
        # authorities: DirServer and AlternateDirAuthority.
        
        # TODO: Both options accept a set of flags to more precisely set what they
        # overwrite. Ideally this would account for these flags to more accurately
        # identify authority connections from relays.
        
        dirServerCfg = self.getOption("DirServer", [], True)
        altDirAuthCfg = self.getOption("AlternateDirAuthority", [], True)
        altAuthoritiesCfg = dirServerCfg + altDirAuthCfg
        
        if altAuthoritiesCfg:
          result = []
          
          # entries are of the form:
          # [nickname] [flags] address:port fingerprint
          for entry in altAuthoritiesCfg:
            locationComp = entry.split()[-2] # address:port component
            result.append(tuple(locationComp.split(":", 1)))
        else: result = list(DIR_SERVERS)
      
      # cache value
      if result: self._cachedParam[key] = result
      elif cacheUndefined: self._cachedParam[key] = UNKNOWN
    elif currentVal == UNKNOWN: result = currentVal
    
    self.connLock.release()
    
    if result: return result
    else: return default
  
  def _notifyStatusListeners(self, eventType):
    """
    Sends a notice to all current listeners that a given change in tor's
    controller status has occurred.
    
    Arguments:
      eventType - enum representing tor's new status
    """
    
    # resets cached GETINFO and GETCONF parameters
    self._cachedParam = dict([(arg, "") for arg in CACHE_ARGS])
    self._cachedConf = {}
    
    # gives a notice that the control port has closed
    if eventType == State.CLOSED:
      log.log(CONFIG["log.torCtlPortClosed"], "Tor control port closed")
    
    for callback in self.statusListeners:
      callback(self, eventType)

class ExitPolicy:
  """
  Single rule from the user's exit policy. These are chained together to form
  complete policies.
  """
  
  def __init__(self, ruleEntry, nextRule):
    """
    Exit policy rule constructor.
    
    Arguments:
      ruleEntry - tor exit policy rule (for instance, "reject *:135-139")
      nextRule  - next rule to be checked when queries don't match this policy
    """
    
    # sanitize the input a bit, cleaning up tabs and stripping quotes
    ruleEntry = ruleEntry.replace("\\t", " ").replace("\"", "")
    
    self.ruleEntry = ruleEntry
    self.nextRule = nextRule
    self.isAccept = ruleEntry.startswith("accept")
    
    # strips off "accept " or "reject " and extra spaces
    ruleEntry = ruleEntry[7:].replace(" ", "")
    
    # split ip address (with mask if provided) and port
    if ":" in ruleEntry: entryIp, entryPort = ruleEntry.split(":", 1)
    else: entryIp, entryPort = ruleEntry, "*"
    
    # sets the ip address component
    self.isIpWildcard = entryIp == "*" or entryIp.endswith("/0")
    
    # checks for the private alias (which expands this to a chain of entries)
    if entryIp.lower() == "private":
      entryIp = PRIVATE_IP_RANGES[0]
      
      # constructs the chain backwards (last first)
      lastHop = self.nextRule
      prefix = "accept " if self.isAccept else "reject "
      suffix = ":" + entryPort
      for addr in PRIVATE_IP_RANGES[-1:0:-1]:
        lastHop = ExitPolicy(prefix + addr + suffix, lastHop)
      
      self.nextRule = lastHop # our next hop is the start of the chain
    
    if "/" in entryIp:
      ipComp = entryIp.split("/", 1)
      self.ipAddress = ipComp[0]
      self.ipMask = int(ipComp[1])
    else:
      self.ipAddress = entryIp
      self.ipMask = 32
    
    # constructs the binary address just in case of comparison with a mask
    if self.ipAddress != "*":
      self.ipAddressBin = ""
      for octet in self.ipAddress.split("."):
        # Converts the int to a binary string, padded with zeros. Source:
        # http://www.daniweb.com/code/snippet216539.html
        self.ipAddressBin += "".join([str((int(octet) >> y) & 1) for y in range(7, -1, -1)])
    else:
      self.ipAddressBin = "0" * 32
    
    # sets the port component
    self.minPort, self.maxPort = 0, 0
    self.isPortWildcard = entryPort == "*"
    
    if entryPort != "*":
      if "-" in entryPort:
        portComp = entryPort.split("-", 1)
        self.minPort = int(portComp[0])
        self.maxPort = int(portComp[1])
      else:
        self.minPort = int(entryPort)
        self.maxPort = int(entryPort)
    
    # if both the address and port are wildcards then we're effectively the
    # last entry so cut off the remaining chain
    if self.isIpWildcard and self.isPortWildcard:
      self.nextRule = None
  
  def check(self, ipAddress, port):
    """
    Checks if the rule chain allows exiting to this address, returning true if
    so and false otherwise.
    """
    
    port = int(port)
    
    # does the port check first since comparing ip masks is more work
    isPortMatch = self.isPortWildcard or (port >= self.minPort and port <= self.maxPort)
    
    if isPortMatch:
      isIpMatch = self.isIpWildcard or self.ipAddress == ipAddress
      
      # expands the check to include the mask if it has one
      if not isIpMatch and self.ipMask != 32:
        inputAddressBin = ""
        for octet in ipAddress.split("."):
          inputAddressBin += "".join([str((int(octet) >> y) & 1) for y in range(7, -1, -1)])
        
        isIpMatch = self.ipAddressBin[:self.ipMask] == inputAddressBin[:self.ipMask]
      
      if isIpMatch: return self.isAccept
    
    # our policy doesn't concern this address, move on to the next one
    if self.nextRule: return self.nextRule.check(ipAddress, port)
    else: return True # fell off the chain without a conclusion (shouldn't happen...)
  
  def __str__(self):
    # This provides the actual policy rather than the entry used to construct
    # it so the 'private' keyword is expanded.
    
    acceptanceLabel = "accept" if self.isAccept else "reject"
    
    if self.isIpWildcard:
      ipLabel = "*"
    elif self.ipMask != 32:
      ipLabel = "%s/%i" % (self.ipAddress, self.ipMask)
    else: ipLabel = self.ipAddress
    
    if self.isPortWildcard:
      portLabel = "*"
    elif self.minPort != self.maxPort:
      portLabel = "%i-%i" % (self.minPort, self.maxPort)
    else: portLabel = str(self.minPort)
    
    myPolicy = "%s %s:%s" % (acceptanceLabel, ipLabel, portLabel)
    
    if self.nextRule:
      return myPolicy + ", " + str(self.nextRule)
    else: return myPolicy

