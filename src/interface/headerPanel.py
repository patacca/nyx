"""
Top panel for every page, containing basic system and tor related information.
If there's room available then this expands to present its information in two
columns, otherwise it's laid out as follows:
  arm - <hostname> (<os> <sys/version>)         Tor <tor/version> (<new, old, recommended, etc>)
  <nickname> - <address>:<orPort>, [Dir Port: <dirPort>, ]Control Port (<open, password, cookie>): <controlPort>
  cpu: <cpu%> mem: <mem> (<mem%>) uid: <uid> uptime: <upmin>:<upsec>
  fingerprint: <fingerprint>

Example:
  arm - odin (Linux 2.6.24-24-generic)         Tor 0.2.1.19 (recommended)
  odin - 76.104.132.98:9001, Dir Port: 9030, Control Port (cookie): 9051
  cpu: 14.6%    mem: 42 MB (4.2%)    pid: 20060   uptime: 48:27
  fingerprint: BDAD31F6F318E0413833E8EBDA956F76E4D66788
"""

import os
import time
import threading

from util import panel, sysTools, torTools, uiTools

# minimum width for which panel attempts to double up contents (two columns to
# better use screen real estate)
MIN_DUAL_COL_WIDTH = 141

FLAG_COLORS = {"Authority": "white",  "BadExit": "red",     "BadDirectory": "red",    "Exit": "cyan",
               "Fast": "yellow",      "Guard": "green",     "HSDir": "magenta",       "Named": "blue",
               "Stable": "blue",      "Running": "yellow",  "Unnamed": "magenta",     "Valid": "green",
               "V2Dir": "cyan",       "V3Dir": "white"}

VERSION_STATUS_COLORS = {"new": "blue", "new in series": "blue", "obsolete": "red", "recommended": "green",  
                         "old": "red",  "unrecommended": "red",  "unknown": "cyan"}

class HeaderPanel(panel.Panel, threading.Thread):
  """
  Top area contenting tor settings and system information. Stats are stored in
  the vals mapping, keys including:
    tor/  version, versionStatus, nickname, orPort, dirPort, controlPort,
          exitPolicy, isAuthPassword (bool), isAuthCookie (bool),
          orListenAddr, *address, *fingerprint, *flags, pid, startTime
    sys/  hostname, os, version
    stat/ *%torCpu, *%armCpu, *rss, *%mem
  
  * volatile parameter that'll be reset on each update
  """
  
  def __init__(self, stdscr, startTime):
    panel.Panel.__init__(self, stdscr, "header", 0)
    threading.Thread.__init__(self)
    self.setDaemon(True)
    
    self._isTorConnected = True
    self._lastUpdate = -1       # time the content was last revised
    self._isPaused = False      # prevents updates if true
    self._halt = False          # terminates thread if true
    self._cond = threading.Condition()  # used for pausing the thread
    
    # Time when the panel was paused or tor was stopped. This is used to
    # freeze the uptime statistic (uptime increments normally when None).
    self._haltTime = None
    
    # The last arm cpu usage sampling taken. This is a tuple of the form:
    # (total arm cpu time, sampling timestamp)
    # 
    # The initial cpu total should be zero. However, at startup the cpu time
    # in practice is often greater than the real time causing the initially
    # reported cpu usage to be over 100% (which shouldn't be possible on
    # single core systems).
    # 
    # Setting the initial cpu total to the value at this panel's init tends to
    # give smoother results (staying in the same ballpark as the second
    # sampling) so fudging the numbers this way for now.
    
    self._armCpuSampling = (sum(os.times()[:3]), startTime)
    
    # Last sampling received from the ResourceTracker, used to detect when it
    # changes.
    self._lastResourceFetch = -1
    
    self.vals = {}
    self.valsLock = threading.RLock()
    self._update(True)
    
    # listens for tor reload (sighup) events
    torTools.getConn().addStatusListener(self.resetListener)
  
  def getHeight(self):
    """
    Provides the height of the content, which is dynamically determined by the
    panel's maximum width.
    """
    
    isWide = self.getParent().getmaxyx()[1] >= MIN_DUAL_COL_WIDTH
    if self.vals["tor/orPort"]: return 4 if isWide else 6
    else: return 3 if isWide else 4
  
  def draw(self, subwindow, width, height):
    self.valsLock.acquire()
    isWide = width + 1 >= MIN_DUAL_COL_WIDTH
    
    # space available for content
    if isWide:
      leftWidth = max(width / 2, 77)
      rightWidth = width - leftWidth
    else: leftWidth = rightWidth = width
    
    # Line 1 / Line 1 Left (system and tor version information)
    sysNameLabel = "arm - %s" % self.vals["sys/hostname"]
    contentSpace = min(leftWidth, 40)
    
    if len(sysNameLabel) + 10 <= contentSpace:
      sysTypeLabel = "%s %s" % (self.vals["sys/os"], self.vals["sys/version"])
      sysTypeLabel = uiTools.cropStr(sysTypeLabel, contentSpace - len(sysNameLabel) - 3, 4)
      self.addstr(0, 0, "%s (%s)" % (sysNameLabel, sysTypeLabel))
    else:
      self.addstr(0, 0, uiTools.cropStr(sysNameLabel, contentSpace))
    
    contentSpace = leftWidth - 43
    if 7 + len(self.vals["tor/version"]) + len(self.vals["tor/versionStatus"]) <= contentSpace:
      versionColor = VERSION_STATUS_COLORS[self.vals["tor/versionStatus"]] if \
          self.vals["tor/versionStatus"] in VERSION_STATUS_COLORS else "white"
      versionStatusMsg = "<%s>%s</%s>" % (versionColor, self.vals["tor/versionStatus"], versionColor)
      self.addfstr(0, 43, "Tor %s (%s)" % (self.vals["tor/version"], versionStatusMsg))
    elif 11 <= contentSpace:
      self.addstr(0, 43, uiTools.cropStr("Tor %s" % self.vals["tor/version"], contentSpace, 4))
    
    # Line 2 / Line 2 Left (tor ip/port information)
    if self.vals["tor/orPort"]:
      myAddress = "Unknown"
      if self.vals["tor/orListenAddr"]: myAddress = self.vals["tor/orListenAddr"]
      elif self.vals["tor/address"]: myAddress = self.vals["tor/address"]
      
      # acting as a relay (we can assume certain parameters are set
      entry = ""
      dirPortLabel = ", Dir Port: %s" % self.vals["tor/dirPort"] if self.vals["tor/dirPort"] != "0" else ""
      for label in (self.vals["tor/nickname"], " - " + myAddress, ":" + self.vals["tor/orPort"], dirPortLabel):
        if len(entry) + len(label) <= leftWidth: entry += label
        else: break
    else:
      # non-relay (client only)
      # TODO: not sure what sort of stats to provide...
      entry = "<red><b>Relaying Disabled</b></red>"
    
    if self.vals["tor/isAuthPassword"]: authType = "password"
    elif self.vals["tor/isAuthCookie"]: authType = "cookie"
    else: authType = "open"
    
    if len(entry) + 19 + len(self.vals["tor/controlPort"]) + len(authType) <= leftWidth:
      authColor = "red" if authType == "open" else "green"
      authLabel = "<%s>%s</%s>" % (authColor, authType, authColor)
      self.addfstr(1, 0, "%s, Control Port (%s): %s" % (entry, authLabel, self.vals["tor/controlPort"]))
    elif len(entry) + 16 + len(self.vals["tor/controlPort"]) <= leftWidth:
      self.addstr(1, 0, "%s, Control Port: %s" % (entry, self.vals["tor/controlPort"]))
    else: self.addstr(1, 0, entry)
    
    # Line 3 / Line 1 Right (system usage info)
    y, x = (0, leftWidth) if isWide else (2, 0)
    if self.vals["stat/rss"] != "0": memoryLabel = uiTools.getSizeLabel(int(self.vals["stat/rss"]))
    else: memoryLabel = "0"
    
    uptimeLabel = ""
    if self.vals["tor/startTime"]:
      if self._haltTime:
        # freeze the uptime when paused or the tor process is stopped
        uptimeLabel = uiTools.getShortTimeLabel(self._haltTime - self.vals["tor/startTime"])
      else:
        uptimeLabel = uiTools.getShortTimeLabel(time.time() - self.vals["tor/startTime"])
    
    sysFields = ((0, "cpu: %s%% tor, %s%% arm" % (self.vals["stat/%torCpu"], self.vals["stat/%armCpu"])),
                 (27, "mem: %s (%s%%)" % (memoryLabel, self.vals["stat/%mem"])),
                 (47, "pid: %s" % (self.vals["tor/pid"] if self._isTorConnected else "")),
                 (59, "uptime: %s" % uptimeLabel))
    
    for (start, label) in sysFields:
      if start + len(label) <= rightWidth: self.addstr(y, x + start, label)
      else: break
    
    if self.vals["tor/orPort"]:
      # Line 4 / Line 2 Right (fingerprint)
      y, x = (1, leftWidth) if isWide else (3, 0)
      fingerprintLabel = uiTools.cropStr("fingerprint: %s" % self.vals["tor/fingerprint"], width)
      self.addstr(y, x, fingerprintLabel)
      
      # Line 5 / Line 3 Left (flags)
      if self._isTorConnected:
        flagLine = "flags: "
        for flag in self.vals["tor/flags"]:
          flagColor = FLAG_COLORS[flag] if flag in FLAG_COLORS.keys() else "white"
          flagLine += "<b><%s>%s</%s></b>, " % (flagColor, flag, flagColor)
        
        if len(self.vals["tor/flags"]) > 0: flagLine = flagLine[:-2]
        else: flagLine += "<b><cyan>none</cyan></b>"
        
        self.addfstr(2 if isWide else 4, 0, flagLine)
      else:
        statusTime = torTools.getConn().getStatus()[1]
        statusTimeLabel = time.strftime("%H:%M %m/%d/%Y", time.localtime(statusTime))
        self.addfstr(2 if isWide else 4, 0, "<b><red>Tor Disconnected</red></b> (%s)" % statusTimeLabel)
      
      # Undisplayed / Line 3 Right (exit policy)
      if isWide:
        exitPolicy = self.vals["tor/exitPolicy"]
        
        # adds note when default exit policy is appended
        if exitPolicy == "": exitPolicy = "<default>"
        elif not exitPolicy.endswith((" *:*", " *")): exitPolicy += ", <default>"
        
        # color codes accepts to be green, rejects to be red, and default marker to be cyan
        isSimple = len(exitPolicy) > rightWidth - 13
        policies = exitPolicy.split(", ")
        for i in range(len(policies)):
          policy = policies[i].strip()
          displayedPolicy = policy.replace("accept", "").replace("reject", "").strip() if isSimple else policy
          if policy.startswith("accept"): policy = "<green><b>%s</b></green>" % displayedPolicy
          elif policy.startswith("reject"): policy = "<red><b>%s</b></red>" % displayedPolicy
          elif policy.startswith("<default>"): policy = "<cyan><b>%s</b></cyan>" % displayedPolicy
          policies[i] = policy
        
        self.addfstr(2, leftWidth, "exit policy: %s" % ", ".join(policies))
    else:
      # Client only
      # TODO: not sure what information to provide here...
      pass
    
    self.valsLock.release()
  
  def setPaused(self, isPause):
    """
    If true, prevents updates from being presented.
    """
    
    if not self._isPaused == isPause:
      self._isPaused = isPause
      if self._isTorConnected:
        if isPause: self._haltTime = time.time()
        else: self._haltTime = None
      
      # Redraw now so we'll be displaying the state right when paused
      # (otherwise the uptime might be off by a second, and change when
      # the panel's redrawn for other reasons).
      self.redraw(True)
  
  def run(self):
    """
    Keeps stats updated, checking for new information at a set rate.
    """
    
    lastDraw = time.time() - 1
    while not self._halt:
      currentTime = time.time()
      
      if self._isPaused or currentTime - lastDraw < 1 or not self._isTorConnected:
        self._cond.acquire()
        if not self._halt: self._cond.wait(0.2)
        self._cond.release()
      else:
        # Update the volatile attributes (cpu, memory, flags, etc) if we have
        # a new resource usage sampling (the most dynamic stat) or its been
        # twenty seconds since last fetched (so we still refresh occasionally
        # when resource fetches fail).
        # 
        # Otherwise, just redraw the panel to change the uptime field.
        
        isChanged = False
        if self.vals["tor/pid"]:
          resourceTracker = sysTools.getResourceTracker(self.vals["tor/pid"])
          isChanged = self._lastResourceFetch != resourceTracker.getRunCount()
        
        if isChanged or currentTime - self._lastUpdate >= 20:
          self._update()
        
        self.redraw(True)
        lastDraw += 1
  
  def stop(self):
    """
    Halts further resolutions and terminates the thread.
    """
    
    self._cond.acquire()
    self._halt = True
    self._cond.notifyAll()
    self._cond.release()
  
  def resetListener(self, conn, eventType):
    """
    Updates static parameters on tor reload (sighup) events.
    
    Arguments:
      conn      - tor controller
      eventType - type of event detected
    """
    
    if eventType == torTools.State.INIT:
      self._isTorConnected = True
      if self._isPaused: self._haltTime = time.time()
      else: self._haltTime = None
      
      self._update(True)
      self.redraw(True)
    elif eventType == torTools.State.CLOSED:
      self._isTorConnected = False
      self._haltTime = time.time()
      self._update()
      self.redraw(True)
  
  def _update(self, setStatic=False):
    """
    Updates stats in the vals mapping. By default this just revises volatile
    attributes.
    
    Arguments:
      setStatic - resets all parameters, including relatively static values
    """
    
    self.valsLock.acquire()
    conn = torTools.getConn()
    
    if setStatic:
      # version is truncated to first part, for instance:
      # 0.2.2.13-alpha (git-feb8c1b5f67f2c6f) -> 0.2.2.13-alpha
      self.vals["tor/version"] = conn.getInfo("version", "Unknown").split()[0]
      self.vals["tor/versionStatus"] = conn.getInfo("status/version/current", "Unknown")
      self.vals["tor/nickname"] = conn.getOption("Nickname", "")
      self.vals["tor/orPort"] = conn.getOption("ORPort", "0")
      self.vals["tor/dirPort"] = conn.getOption("DirPort", "0")
      self.vals["tor/controlPort"] = conn.getOption("ControlPort", "")
      self.vals["tor/isAuthPassword"] = conn.getOption("HashedControlPassword") != None
      self.vals["tor/isAuthCookie"] = conn.getOption("CookieAuthentication") == "1"
      
      # orport is reported as zero if unset
      if self.vals["tor/orPort"] == "0": self.vals["tor/orPort"] = ""
      
      # overwrite address if ORListenAddress is set (and possibly orPort too)
      self.vals["tor/orListenAddr"] = ""
      listenAddr = conn.getOption("ORListenAddress")
      if listenAddr:
        if ":" in listenAddr:
          # both ip and port overwritten
          self.vals["tor/orListenAddr"] = listenAddr[:listenAddr.find(":")]
          self.vals["tor/orPort"] = listenAddr[listenAddr.find(":") + 1:]
        else:
          self.vals["tor/orListenAddr"] = listenAddr
      
      # fetch exit policy (might span over multiple lines)
      policyEntries = []
      for exitPolicy in conn.getOption("ExitPolicy", [], True):
        policyEntries += [policy.strip() for policy in exitPolicy.split(",")]
      self.vals["tor/exitPolicy"] = ", ".join(policyEntries)
      
      # system information
      unameVals = os.uname()
      self.vals["sys/hostname"] = unameVals[1]
      self.vals["sys/os"] = unameVals[0]
      self.vals["sys/version"] = unameVals[2]
      
      pid = conn.getMyPid()
      self.vals["tor/pid"] = pid if pid else ""
      
      startTime = conn.getStartTime()
      self.vals["tor/startTime"] = startTime if startTime else ""
      
      # reverts volatile parameters to defaults
      self.vals["tor/fingerprint"] = "Unknown"
      self.vals["tor/flags"] = []
      self.vals["stat/%torCpu"] = "0"
      self.vals["stat/%armCpu"] = "0"
      self.vals["stat/rss"] = "0"
      self.vals["stat/%mem"] = "0"
    
    # sets volatile parameters
    # TODO: This can change, being reported by STATUS_SERVER -> EXTERNAL_ADDRESS
    # events. Introduce caching via torTools?
    self.vals["tor/address"] = conn.getInfo("address", "")
    
    self.vals["tor/fingerprint"] = conn.getInfo("fingerprint", self.vals["tor/fingerprint"])
    self.vals["tor/flags"] = conn.getMyFlags(self.vals["tor/flags"])
    
    # ps or proc derived resource usage stats
    if self.vals["tor/pid"]:
      resourceTracker = sysTools.getResourceTracker(self.vals["tor/pid"])
      
      if resourceTracker.lastQueryFailed():
        self.vals["stat/%torCpu"] = "0"
        self.vals["stat/rss"] = "0"
        self.vals["stat/%mem"] = "0"
      else:
        cpuUsage, _, memUsage, memUsagePercent = resourceTracker.getResourceUsage()
        self._lastResourceFetch = resourceTracker.getRunCount()
        self.vals["stat/%torCpu"] = "%0.1f" % (100 * cpuUsage)
        self.vals["stat/rss"] = str(memUsage)
        self.vals["stat/%mem"] = "%0.1f" % (100 * memUsagePercent)
    
    # determines the cpu time for the arm process (including user and system
    # time of both the primary and child processes)
    
    totalArmCpuTime, currentTime = sum(os.times()[:3]), time.time()
    armCpuDelta = totalArmCpuTime - self._armCpuSampling[0]
    armTimeDelta = currentTime - self._armCpuSampling[1]
    pythonCpuTime = armCpuDelta / armTimeDelta
    sysCallCpuTime = sysTools.getSysCpuUsage()
    self.vals["stat/%armCpu"] = "%0.1f" % (100 * (pythonCpuTime + sysCallCpuTime))
    self._armCpuSampling = (totalArmCpuTime, currentTime)
    
    self._lastUpdate = currentTime
    self.valsLock.release()

