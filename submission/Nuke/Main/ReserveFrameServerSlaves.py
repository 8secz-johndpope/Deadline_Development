from __future__ import print_function
import os
import sys
import re
import traceback
import subprocess
import ast
import socket

from subprocess import call
from threading import Thread
from time import sleep

try:
    # Nuke 11 onwards uses PySide2
    from PySide2 import QtGui, QtCore
except:
    from PySide import QtGui, QtCore

try:
    import ConfigParser
except:
    print( "Could not load ConfigParser module, sticky settings will not be loaded/saved" )

import nuke
import nukescripts

dialog = None
nukeScriptPath = None
deadlineHome = None
deadlineTemp = None
configFile = None

import DeadlineFRGlobals as DeadlineGlobals

class ReserveFrameServerSlavesDialog( nukescripts.PythonPanel ):
    pools = []
    groups = []

    def __init__( self, maximumPriority, pools, secondaryPools, groups ):
        super( ReserveFrameServerSlavesDialog, self ).__init__( "Setup Frame Server Slaves With Deadline", "com.thinkboxsoftware.software.deadlinefrdialog" )

        # The environment variables themselves are integers. But since we can't test Nuke 6 to ensure they exist,
        # we have to use their `GlobalsEnvironment`, not a dict, get method which only accepts strings as defaults.
        self.nukeVersion = ( int( nuke.env.get( 'NukeVersionMajor', '6' ) ),
                             int( nuke.env.get( 'NukeVersionMinor', '0' ) ),
                             int( nuke.env.get( 'NukeVersionRelease', '0' ) ), )

        width = 605
        height = 710
        self.setMinimumSize( width, height )

        self.JobID = None
        # In Nuke 11.2.X, Adding a Tab_Knob and showing the dialog hard crashes Nuke. With only 1 tab, we can just remove it.
        if self.nukeVersion < ( 11, 2 ):
            self.jobTab = nuke.Tab_Knob( "DeadlineFR_JobOptionsTab", "Job Options" )
            self.addKnob( self.jobTab )
        
        ##########################################################################################
        ## Job Description
        ##########################################################################################
        
        # Job Name
        self.jobName = nuke.String_Knob( "DeadlineFR_JobName", "Job Name" )
        self.addKnob( self.jobName )
        self.jobName.setTooltip( "The name of your job. This is optional, and if left blank, it will default to 'Untitled'." )
        self.jobName.setValue( "Untitled" )
        
        # Comment
        self.comment = nuke.String_Knob( "DeadlineFR_Comment", "Comment" )
        self.addKnob( self.comment )
        self.comment.setTooltip( "A simple description of your job. This is optional and can be left blank." )
        self.comment.setValue( "" )
        
        # Department
        self.department = nuke.String_Knob( "DeadlineFR_Department", "Department" )
        self.addKnob( self.department )
        self.department.setTooltip( "The department you belong to. This is optional and can be left blank." )
        self.department.setValue( "" )
        
        # Separator
        self.separator1 = nuke.Text_Knob( "DeadlineFR_Separator1", "" )
        self.addKnob( self.separator1 )
        
        ##########################################################################################
        ## Job Scheduling
        ##########################################################################################
        
        # Pool
        self.pool = nuke.Enumeration_Knob( "DeadlineFR_Pool", "Pool", pools )
        self.addKnob( self.pool )
        self.pool.setTooltip( "The pool that your job will be submitted to." )
        self.pool.setValue( "none" )
        
        # Secondary Pool
        self.secondaryPool = nuke.Enumeration_Knob( "DeadlineFR_SecondaryPool", "Secondary Pool", secondaryPools )
        self.addKnob( self.secondaryPool )
        self.secondaryPool.setTooltip( "The secondary pool lets you specify a Pool to use if the primary Pool does not have any available Slaves." )
        self.secondaryPool.setValue( " " )
        
        # Group
        self.group = nuke.Enumeration_Knob( "DeadlineFR_Group", "Group", groups )
        self.addKnob( self.group )
        self.group.setTooltip( "The group that your job will be submitted to." )
        self.group.setValue( "none" )
        
        # Priority
        self.priority = nuke.Int_Knob( "DeadlineFR_Priority", "Priority" )
        self.addKnob( self.priority )
        self.priority.setTooltip( "A job can have a numeric priority ranging from 0 to " + str(maximumPriority) + ", where 0 is the lowest priority." )
        self.priority.setValue( 50 )
        
        # If the job is interruptible
        self.isInterruptible = nuke.Boolean_Knob( "DeadlineFR_IsInterruptible", "Job Is Interruptible" )
        self.addKnob( self.isInterruptible )
        self.isInterruptible.setTooltip( "If true, the Job can be interrupted during rendering by a Job with higher priority." )
        self.isInterruptible.setValue( False )
        
        # Task Timeout
        self.taskTimeout = nuke.Int_Knob( "DeadlineFR_TaskTimeout", "Task Timeout" )
        self.addKnob( self.taskTimeout )
        self.taskTimeout.setTooltip( "The number of minutes a slave has to render a task for this job before it requeues it. Specify 0 for no limit." )
        self.taskTimeout.setValue( 0 )
        
        # Machine List Is Blacklist
        self.isBlacklist = nuke.Boolean_Knob( "DeadlineFR_IsBlacklist", "Machine List Is A Blacklist" )
        self.addKnob( self.isBlacklist )
        self.isBlacklist.setTooltip( "You can force the job to render on specific machines by using a whitelist, or you can avoid specific machines by using a blacklist." )
        self.isBlacklist.setValue( False )
        
        # Machine List
        self.machineList = nuke.String_Knob( "DeadlineFR_MachineList", "Machine List" )
        self.addKnob( self.machineList )
        self.machineList.setTooltip( "The whitelisted or blacklisted list of machines." )
        self.machineList.setValue( "" )
        
        self.machineListButton = nuke.PyScript_Knob( "DeadlineFR_MachineListButton", "Browse" )
        self.addKnob( self.machineListButton )
        
        # Limit Groups
        self.limitGroups = nuke.String_Knob( "DeadlineFR_LimitGroups", "Limits" )
        self.addKnob( self.limitGroups )
        self.limitGroups.setTooltip( "The Limits that your job requires." )
        self.limitGroups.setValue( "" )
        
        self.limitGroupsButton = nuke.PyScript_Knob( "DeadlineFR_LimitGroupsButton", "Browse" )
        self.addKnob( self.limitGroupsButton )
        
        # Separator
        self.separator2 = nuke.Text_Knob( "DeadlineFR_Separator2", "" )
        self.addKnob( self.separator2 )
        
        # Host name or IP Address
        self.hostName = nuke.String_Knob("DeadlineFR_HostNameIPAddress", "IP Address or Host Name")
        self.addKnob(self.hostName)
        self.hostName.setTooltip("The Host Name or IP Address of the host Nuke Studio machine.")
        self.hostName.setValue("")
        
        # Host Port number to use
        self.port = nuke.Int_Knob( "DeadlineFR_Port", "Port" )
        self.addKnob( self.port )
        self.port.setTooltip( "The Port number to that the host uses." )
        self.port.setValue( 5560 )
        
        #Number of machines to reserve
        self.machines = nuke.Int_Knob( "DeadlineFR_ReserveMachines", "Machines to reserve" )
        self.addKnob( self.machines )
        self.machines.setTooltip( "The number of machines to reserve as workers for the render." )
        self.machines.setValue( 10 )
        
        #Number of workers to launch on each machine
        self.workers = nuke.Int_Knob( "DeadlineFR_Workers", "Workers" )
        self.addKnob( self.workers )
        self.workers.setTooltip( "The number of workers to launch on each machine." )
        self.workers.setValue( 1 )
        
        #Number of threads each worker is allocated
        self.workerThreads = nuke.Int_Knob( "DeadlineFR_WorkerThreads", "Worker Threads" )
        self.addKnob( self.workerThreads )
        self.workerThreads.setTooltip( "The number of worker threads to start for each worker." )
        self.workerThreads.setValue( 1 )
        
        #Worker memory override
        self.workerMem = nuke.Int_Knob( "DeadlineFR_WorkerMem", "Worker Memory" )
        self.addKnob( self.workerMem )
        self.workerMem.setTooltip( "The amount of memory, in MB, allocated to each frame server worker" )
        self.workerMem.setValue( 4096 )
        
        # Separator
        self.separator3 = nuke.Text_Knob( "DeadlineFR_Separator3", "" )
        self.addKnob( self.separator3 )
        
        self.jobID = nuke.String_Knob("DeadlineFR_JobID", "Job ID")
        self.addKnob(self.jobID)
        self.jobID.setTooltip("The ID of the Job.")
        self.jobID.setFlag( nuke.READ_ONLY )
        self.jobID.setEnabled(False)
        
        self.jobStatus = nuke.String_Knob("DeadlineFR_JobStatus", "Job Status")
        self.addKnob(self.jobStatus)
        self.jobStatus.setTooltip("The status of the Job.")
        self.jobStatus.setFlag( nuke.READ_ONLY )
        self.jobStatus.setEnabled(False)
        
        self.reservedMachines = nuke.Multiline_Eval_String_Knob( "DeadlineFR_ReservedMachines", "Reserved Machines" )
        self.addKnob( self.reservedMachines )
        self.reservedMachines.setTooltip("The machines reserved for this job.")
        self.reservedMachines.setFlag( nuke.READ_ONLY )
        self.reservedMachines.setEnabled(False)
        
        self.outputBox = nuke.Multiline_Eval_String_Knob( "DeadlineFR_Output", "Last command output" )
        self.addKnob( self.outputBox )
        self.outputBox.setTooltip("The output from the last action, if any.")
        self.outputBox.setFlag( nuke.READ_ONLY )
        self.outputBox.setEnabled(False)
        
        #Submits a job that reserves machines
        self.reserveButton = nuke.PyScript_Knob( "DeadlineFR_ReserveButton", "Reserve Machines" )
        self.addKnob( self.reserveButton )
        self.reserveButton.setTooltip( "Submits the Job to Deadline, which reserves some slaves for the frame server." )
        self.reserveButton.setFlag( nuke.STARTLINE )
        
        #If there is a job submitted, mark it as complete
        self.freeButton = nuke.PyScript_Knob("DeadlineFR_FreeButton", "Release Machines" )
        self.addKnob( self.freeButton)
        self.freeButton.setTooltip( "Sets the Deadline Job to complete and frees up the slaves reserved for the frame server.")
        self.freeButton.setEnabled(False)
        
        self.launchButton = nuke.PyScript_Knob( "DeadlineFR_LaunchButton", "Start Deadline Monitor" )
        self.addKnob(self.launchButton)
        self.launchButton.setTooltip( "Launches the Deadline Monitor." )

    def knobChanged( self, knob ):
        if knob == self.machineListButton:
            GetMachineListFromDeadline()
            
        if knob == self.limitGroupsButton:
            GetLimitGroupsFromDeadline()
            
        if knob == self.reserveButton:
            toggleReserveButton()
            updateReserveButton( "Reserving..." )
            Thread( target = SubmitReserveJobToDeadline ).start()
            
        if knob == self.freeButton:
            toggleFreeButton()
            Thread( target = FreeReservedSlaves, args= ( self.JobID, ) ).start()
            self.jobID.setValue("")
            self.jobStatus.setValue("")
            self.reservedMachines.setValue("")
            
        if knob == self.launchButton:
            Thread( target = StartDeadlineMonitor ).start()
            
    def ShowDialog( self ):
        return nukescripts.PythonPanel.show( self )
        
    def close(self):
        Thread( target = FreeReservedSlaves, args=( self.JobID, True ) ).start()
        WriteStickySettings()
        os.environ['ReserveOpen'] = "F"
        return super(ReserveFrameServerSlavesDialog, self).close()

def updateJobID( jobId ):
    global dialog
    if dialog is not None and dialog.freeButton.enabled():
        dialog.jobID.setValue( str( dialog.JobID ) )

def updateReservedMachines( servers ):
    global dialog
    if dialog is not None and dialog.reservedMachines is not None and dialog.freeButton.enabled():
        dialog.reservedMachines.setValue( servers )

def updateJobStatus( status ):
    global dialog
    if dialog is not None and dialog.freeButton.enabled():
        dialog.jobStatus.setValue( status )

def updateOutputBox( message ):
    global dialog
    if dialog is not None:
        dialog.outputBox.setValue( message )

def toggleReserveButton():
    global dialog
    dialog.reserveButton.setEnabled( not dialog.reserveButton.enabled() )

def toggleFreeButton():
    global dialog
    dialog.freeButton.setEnabled( not dialog.freeButton.enabled() )

def updateReserveButton( newLabel ):
    global dialog
    dialog.reserveButton.setLabel( newLabel )

def toggleFields():
    global dialog

    dialog.jobID.setEnabled( not dialog.jobID.enabled() )
    dialog.jobStatus.setEnabled( not dialog.jobStatus.enabled() )
    dialog.reservedMachines.setEnabled( not dialog.reservedMachines.enabled() )
    dialog.outputBox.setEnabled( not dialog.outputBox.enabled() )

def showNukeMessage( message ):
    nuke.message( message )

def updateList():
    global dialog

    jobID = dialog.JobID

    while jobID != "" and jobID is not None:
        nuke.executeInMainThread( updateJobID, jobID )

        servers = CallDeadlineCommand( ["GetSlavesRenderingJob", jobID] )
        
        nuke.executeInMainThread( updateReservedMachines, servers )
            
        job = CallDeadlineCommand( ["-getjob", jobID] )

        if "JobStatus=" in job:
            status = job.split("JobStatus=")[1].split("\n")[0]

            if status == "Active":
                if len( servers ) > 0:
                    status = "Rendering" 
                else:
                    status = "Queued"
            elif status == "":
                status = "Deleted"

            nuke.executeInMainThread( updateJobStatus, status )            
        
        sleep( 5.0 )

        if dialog is not None:
            jobID = dialog.JobID
        else:
            jobID = ""

def FreeReservedSlaves(jobID, closed=False):    
    if( jobID != None and jobID != "" ):
    
        job = CallDeadlineCommand(["-getjob", jobID])
        
        if "JobStatus=Active" in job:
    
            output = CallDeadlineCommand(["-completejob", jobID])
            message = "Slaves released for job "+str(jobID)
            if closed:
                message = message + " because the dialog was closed!"
                nuke.executeInMainThread( showNukeMessage, message )
            else:
                nuke.executeInMainThread( updateOutputBox, message )
            
        elif dialog is not None:
            nuke.executeInMainThread( updateOutputBox, "Job was not active. Reserved Slaves were previously released." )

        if dialog is not None:
            nuke.executeInMainThread( toggleReserveButton )
            nuke.executeInMainThread( toggleFields )    
        
            dialog.JobID = None

def GetDeadlinePath():
    deadlineBin = ""
    try:
        deadlineBin = os.environ['DEADLINE_PATH']
    except KeyError:
        #if the error is a key error it means that DEADLINE_PATH is not set. however Deadline command may be in the PATH or on OSX it could be in the file /Users/Shared/Thinkbox/DEADLINE_PATH
        pass
        
    # On OSX, we look for the DEADLINE_PATH file if the environment variable does not exist.
    if deadlineBin == "" and  os.path.exists( "/Users/Shared/Thinkbox/DEADLINE_PATH" ):
        with open( "/Users/Shared/Thinkbox/DEADLINE_PATH" ) as f:
            deadlineBin = f.read().strip()
    
    return deadlineBin
            
def StartDeadlineMonitor():
    deadlineCommand = os.path.join( GetDeadlinePath(), "deadlinemonitor" )
            
    environment = {}
    for key in os.environ.keys():
        environment[key] = str(os.environ[key])
        
    # Need to set the PATH, cuz windows seems to load DLLs from the PATH earlier that cwd....
    if os.name == 'nt':
        deadlineCommandDir = os.path.dirname( deadlineCommand )
        if not deadlineCommandDir == "" :
            environment['PATH'] = deadlineCommandDir + os.pathsep + os.environ['PATH']
    
    # Specifying PIPE for all handles to workaround a Python bug on Windows. The unused handles are then closed immediatley afterwards.
    proc = subprocess.Popen([deadlineMonitor], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    proc.stdin.close()
    proc.stderr.close()
    proc.stdout.close()
    
def CallDeadlineCommand( arguments, hideWindow=True ):
    deadlineCommand = os.path.join( GetDeadlinePath(), "deadlinecommand" )
    
    startupinfo = None
    if hideWindow and os.name == 'nt':
        # Python 2.6 has subprocess.STARTF_USESHOWWINDOW, and Python 2.7 has subprocess._subprocess.STARTF_USESHOWWINDOW, so check for both.
        if hasattr( subprocess, '_subprocess' ) and hasattr( subprocess._subprocess, 'STARTF_USESHOWWINDOW' ):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess._subprocess.STARTF_USESHOWWINDOW
        elif hasattr( subprocess, 'STARTF_USESHOWWINDOW' ):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    
    environment = {}
    for key in os.environ.keys():
        environment[key] = str(os.environ[key])
        
    # Need to set the PATH, cuz windows seems to load DLLs from the PATH earlier that cwd....
    if os.name == 'nt':
        deadlineCommandDir = os.path.dirname( deadlineCommand )
        if not deadlineCommandDir == "" :
            environment['PATH'] = deadlineCommandDir + os.pathsep + os.environ['PATH']
    
    arguments.insert( 0, deadlineCommand)
    
    # Specifying PIPE for all handles to workaround a Python bug on Windows. The unused handles are then closed immediatley afterwards.
    proc = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo, env=environment)
    proc.stdin.close()
    proc.stderr.close()
    
    output = proc.stdout.read()
    
    return output
    
def GetMachineListFromDeadline():
    global dialog
    output = CallDeadlineCommand( ["-selectmachinelist", dialog.machineList.value()], False )
    output = output.replace( "\r", "" ).replace( "\n", "" )
    if output != "Action was cancelled by user":
        dialog.machineList.setValue( output )
    
def GetLimitGroupsFromDeadline():
    global dialog
    output = CallDeadlineCommand( ["-selectlimitgroups", dialog.limitGroups.value()], False )
    output = output.replace( "\r", "" ).replace( "\n", "" )
    if output != "Action was cancelled by user":
        dialog.limitGroups.setValue( output )
        
def WriteStickySettings():
    global configFile
    global dialog
    try:
        print( "Writing sticky settings..." )
        config = ConfigParser.ConfigParser()
        config.add_section( "Sticky" )
        
        config.set( "Sticky", "Department", dialog.department.value() )
        config.set( "Sticky", "Pool", dialog.pool.value() )
        config.set( "Sticky", "Group", dialog.group.value() )
        config.set( "Sticky", "Priority", str( dialog.priority.value() ) )
        config.set( "Sticky", "IsBlacklist", str( dialog.isBlacklist.value() ) )
        config.set( "Sticky", "MachineList", dialog.machineList.value() )
        config.set( "Sticky", "LimitGroups", dialog.limitGroups.value() )
        config.set( "Sticky", "TaskTimeout", str(dialog.taskTimeout.value()) )
        config.set( "Sticky", "IsInterruptible", str(dialog.isInterruptible.value() ) )
        config.set( "Sticky", "HostName", dialog.hostName.value() )
        config.set( "Sticky", "Port", str(dialog.port.value()) )
        config.set( "Sticky", "MachineCount", str(dialog.machines.value()) )
        config.set( "Sticky", "WorkerCount", str(dialog.workers.value()) )
        config.set( "Sticky", "WorkerThreads", str(dialog.workerThreads.value()) )
        config.set( "Sticky", "WorkerMem", str(dialog.workerMem.value()) )
        
        fileHandle = open( configFile, "w" )
        config.write( fileHandle )
        fileHandle.close()
        
    except:
        print( "Could not write sticky settings" )
        print( traceback.format_exc() )
    
    try:
        #Saves all the sticky setting to the root
        tKnob = buildKnob( "Department", "department" )
        tKnob.setValue( dialog.department.value() )
        
        tKnob = buildKnob( "Pool", "pool" )
        tKnob.setValue( dialog.pool.value() )
        
        tKnob = buildKnob( "Group", "group" )
        tKnob.setValue( dialog.group.value() )
        
        tKnob = buildKnob( "Priority", "priority" )
        tKnob.setValue( str( dialog.priority.value() ) )
        
        tKnob = buildKnob( "IsBlacklist", "isBlacklist" )
        tKnob.setValue( str( dialog.isBlacklist.value() ) )
        
        tKnob = buildKnob( "MachineList", "machineList" )
        tKnob.setValue( dialog.machineList.value() )
        
        tKnob = buildKnob( "LimitGroups", "limitGroups" )
        tKnob.setValue( dialog.limitGroups.value() )
        
        tKnob = buildKnob( "IsInterruptible", "isInterruptible" )
        tKnob.setValue(str(dialog.isInterruptible.value()))
        
        tKnob = buildKnob( "TaskTimeout", "taskTimeout" )
        tKnob.setValue( str(dialog.taskTimeout.value()) )
        
        tKnob = buildKnob( "HostName", "hostName" )
        tKnob.setValue( dialog.hostName.value() )
        
        tKnob = buildKnob( "Port", "Port" )
        tKnob.setValue( str(dialog.port.value()) )
        
        tKnob = buildKnob( "MachineCount", "machines" )
        tKnob.setValue( str(dialog.machines.value()) )
        
        tKnob = buildKnob( "WorkerCount", "workerCount" )
        tKnob.setValue( str(dialog.workers.value()) )
        
        tKnob = buildKnob( "WorkerThreads", "workerThreads" )
        tKnob.setValue( str(dialog.workerThreads.value()) )
        
        tKnob = buildKnob( "WorkerMem", "workerMem" )
        tKnob.setValue( str(dialog.workerMem.value()) )

        root = nuke.Root()
        if root.modified():
            if root.name() != "Root":
                nuke.scriptSave( root.name() )
        
    except:
        print( "Could not write knob settings." )
        print( traceback.format_exc() )
        
    dialog = None
        
def buildKnob( name, abr ):
    try:
        root = nuke.Root()
        if "Deadline" not in root.knobs():
            tabKnob = nuke.Tab_Knob("Deadline")
            root.addKnob(tabKnob)
        
        if name in root.knobs():
            return root.knob( name )
        else:
            tKnob = nuke.String_Knob( name, abr )
            root.addKnob ( tKnob )
            return  tKnob
    except:
        print( "Error in knob creation. " + name + " " + abr )
        
def LoadStickySettings(dialog):
    global configFile
    print( "Reading sticky settings from %s" % configFile )
    try:
        if os.path.isfile( configFile ):
            config = ConfigParser.ConfigParser()
            config.read( configFile )
            
            if config.has_section( "Sticky" ):
                if config.has_option( "Sticky", "Department" ):
                    DeadlineGlobals.initDepartment = config.get( "Sticky", "Department" )
                if config.has_option( "Sticky", "Pool" ):
                    DeadlineGlobals.initPool = config.get( "Sticky", "Pool" )
                if config.has_option( "Sticky", "Group" ):
                    DeadlineGlobals.initGroup = config.get( "Sticky", "Group" )
                if config.has_option( "Sticky", "Priority" ):
                    DeadlineGlobals.initPriority = config.getint( "Sticky", "Priority" )
                if config.has_option( "Sticky", "IsBlacklist" ):
                    DeadlineGlobals.initIsBlacklist = config.getboolean( "Sticky", "IsBlacklist" )
                if config.has_option( "Sticky", "MachineList" ):
                    DeadlineGlobals.initMachineList = config.get( "Sticky", "MachineList" )
                if config.has_option( "Sticky", "LimitGroups" ):
                    DeadlineGlobals.initLimitGroups = config.get( "Sticky", "LimitGroups" )
                if config.has_option("Sticky", "IsInterruptible"):
                    DeadlineGlobals.initIsInterruptible = config.get("Sticky", "IsInterruptible" )
                if config.has_option( "Sticky", "TaskTimeout"):
                    DeadlineGlobals.initTaskTimeout = config.getint( "Sticky", "TaskTimeout" )
                if config.has_option( "Sticky", "HostName"):
                    DeadlineGlobals.initHostName = config.get( "Sticky", "HostName" )
                if config.has_option("Sticky", "Port"):
                    DeadlineGlobals.initPort = config.getint("Sticky", "Port")
                if config.has_option("Sticky", "MachineCount"):
                    DeadlineGlobals.initMachineCount = config.getint("Sticky", "MachineCount" )
                if config.has_option("Sticky", "WorkerCount"):
                    DeadlineGlobals.initWorkerCount = config.getint("Sticky", "WorkerCount")
                if config.has_option("Sticky", "WorkerThreads"):
                    DeadlineGlobals.initWorkerThreads = config.getint("Sticky", "WorkerThreads")
                if config.has_option("Sticky", "WorkerMem"):
                    DeadlineGlobals.initWorkerMem = config.getint("Sticky", "WorkerMem")
                    
    except:
        print( "Could not read sticky settings")
        print( traceback.format_exc() )
    
    try:
        root = nuke.Root()
            
        if "Department" in root.knobs():
            DeadlineGlobals.initDepartment = ( root.knob( "Department" ) ).value()
            
        if "Pool" in root.knobs():
            DeadlineGlobals.initPool = ( root.knob( "Pool" ) ).value()
            
        if "Group" in root.knobs():
            DeadlineGlobals.initGroup = ( root.knob( "Group" ) ).value()
            
        if "Priority" in root.knobs():
            DeadlineGlobals.initPriority = int( ( root.knob( "Priority" ) ).value() )
            
        if "IsBlacklist" in root.knobs():
            DeadlineGlobals.initIsBlacklist = StrToBool( ( root.knob( "IsBlacklist" ) ).value() )
            
        if "IsInterruptible" in root.knobs():
            DeadlineGlobals.initIsInterruptible = StrToBool( (root.knob( "IsInterruptible" )).value() )
        
        if "MachineList" in root.knobs():
            DeadlineGlobals.initMachineList = ( root.knob( "MachineList" ) ).value()
        
        if "LimitGroups" in root.knobs():
            DeadlineGlobals.initLimitGroups = ( root.knob( "LimitGroups" ) ).value()
        
        if "TaskTimeout" in root.knobs():
            DeadlineGlobals.initTaskTimeout = int( ( root.knob( "TaskTimeout" ) ).value() )
            
        if "HostName" in root.knobs():
            DeadlineGlobals.initHostName = ( root.knob( "HostName" ) ).value()  
            
        if "Port" in root.knobs():
            DeadlineGlobals.initPort = int(( root.knob( "Port" ) ).value())
            
        if "MachineCount" in root.knobs():
            DeadlineGlobals.initMachineCount = int(( root.knob( "MachineCount" ) ).value() )
            
        if "WorkerCount" in root.knobs():
            DeadlineGlobals.initWorkerCount = int(( root.knob( "WorkerCount" ) ).value() )
            
        if "WorkerThreads" in root.knobs():
            DeadlineGlobals.initWorkerThreads = int(( root.knob( "WorkerThreads" ) ).value())
            
        if "WorkerMem" in root.knobs():
            DeadlineGlobals.initWorkerMem = int(( root.knob( "WorkerMem" ) ).value())
                
    except:
        print( "Could not read knob settings." )
        print( traceback.format_exc() )
        
def StrToBool(str):
    return str.lower() in ("yes", "true", "t", "1", "on")
        
def SubmitReserveJobToDeadline():
    global dialog
    global deadlineTemp
    
    if str(dialog.hostName.value() ) == "":
        dialog.outputBox.setValue("Need to provide the host name or IP address to connect.")
        return

    jobName = dialog.jobName.value() + " - Frame Server Job ("+str(nuke.env[ 'NukeVersionString' ])+")"
    # Create the submission info file (append job count since we're submitting multiple jobs at the same time in different threads)
    jobInfoFile = deadlineTemp + ("/nuke_reserve_job_info.job")
    fileHandle = open( jobInfoFile, "w" )
    fileHandle.write( "Plugin=NukeFrameServer\n" )
    fileHandle.write( "Name=%s\n" % jobName )
    fileHandle.write( "Comment=%s\n" % dialog.comment.value() )
    fileHandle.write( "Department=%s\n" % dialog.department.value() )
    fileHandle.write( "Pool=%s\n" % dialog.pool.value() )
    if dialog.secondaryPool.value() == " ":
        fileHandle.write( "SecondaryPool=\n" )
    else:
        fileHandle.write( "SecondaryPool=%s\n" % dialog.secondaryPool.value() )
    fileHandle.write( "Group=%s\n" % dialog.group.value() )
    fileHandle.write( "Priority=%s\n" % dialog.priority.value() )
    fileHandle.write( "TaskTimeoutMinutes=%s\n" % dialog.taskTimeout.value() )
    fileHandle.write( "LimitGroups=%s\n" % dialog.limitGroups.value() )

    machineCount = int(dialog.machines.value())
    
    if machineCount <= 1:
        fileHandle.write( "Frames=1\n" )
    else:
        fileHandle.write( "Frames=0-%s\n" % (machineCount-1) )
    fileHandle.write( "ChunkSize=1\n" )
    
    if dialog.isBlacklist.value():
        fileHandle.write( "Blacklist=%s\n" % dialog.machineList.value() )
    else:
        fileHandle.write( "Whitelist=%s\n" % dialog.machineList.value() )
        
    if dialog.isInterruptible.value():
        fileHandle.write("Interruptible=true\n")
    
    fileHandle.close()
    
    # Create the plugin info file
    pluginInfoFile = deadlineTemp + ("/nuke_reserve_plugin_info.job")
    fileHandle = open( pluginInfoFile, "w" )
    
    fileHandle.write( "Version=%s.%s\n" % (nuke.env[ 'NukeVersionMajor' ], nuke.env['NukeVersionMinor']) )
    fileHandle.write( "FrameRenderHost=%s\n" % dialog.hostName.value() )
    fileHandle.write( "FrameRenderPort=%s\n" % dialog.port.value() )
    fileHandle.write( "FrameRenderWorkers=%s\n" % dialog.workers.value() )
    fileHandle.write( "FrameRenderWorkerThreads=%s\n" % dialog.workerThreads.value() )
    fileHandle.write( "FrameRenderWorkerMem=%s\n" % dialog.workerMem.value() )
    fileHandle.close()
    
    # Submit the job to Deadline
    args = []
    args.append( jobInfoFile )
    args.append( pluginInfoFile )
    
    tempResults = ""
    
    tempResults = CallDeadlineCommand( args )
    
    print( "Job submission complete" )
    
    nuke.executeInMainThread( updateOutputBox, tempResults )
    
    #Get the new job ID
    if "JobID=" in tempResults:
        dialog.JobID = tempResults.split("JobID=")[1].split('\n')[0]
    
    if "Success" in tempResults:
        nuke.executeInMainThread( toggleFields )
        nuke.executeInMainThread( toggleFreeButton )

        Thread( target = updateList ).start()
    else:
        nuke.executeInMainThread( toggleReserveButton )

    nuke.executeInMainThread( updateReserveButton, "Reserve Machines" )

    return tempResults
    
def SubmitToDeadline( currNukeScriptPath ):
    try:
        global dialog
        global nukeScriptPath
        global deadlineHome
        global deadlineTemp
        global configFile
        
        # Add the current nuke script path to the system path.
        nukeScriptPath = currNukeScriptPath
        sys.path.append( nukeScriptPath )
        
        # Get the current user Deadline home directory, which we'll use to store settings and temp files.
        deadlineHome = CallDeadlineCommand( ["-GetCurrentUserHomeDirectory",] )
        
        deadlineHome = deadlineHome.replace( "\n", "" ).replace( "\r", "" )
        deadlineSettings = deadlineHome + "/settings"
        deadlineTemp = deadlineHome + "/temp"
        
        configFile = deadlineSettings + "/nuke_studio_py_submission.ini"
        
        # Run the sanity check script if it exists, which can be used to set some initial values.
        sanityCheckFile = nukeScriptPath + "/CustomSanityChecks.py"
        if os.path.isfile( sanityCheckFile ):
            print( "Running sanity check script: " + sanityCheckFile )
            try:
                import CustomSanityChecks
                sanityResult = CustomSanityChecks.RunSanityCheck()
                if not sanityResult:
                    print( "Sanity check returned false, exiting" )
                    return
            except:
                print( "Could not run CustomSanityChecks.py script" )
                print( traceback.format_exc() )
        
        # Get the maximum priority.
        try:
            output = CallDeadlineCommand( ["-getmaximumpriority",] )
            maximumPriority = int(output)
        except:
            maximumPriority = 100
        
        # Get the pools.
        output = CallDeadlineCommand( ["-pools",] )
        pools = output.splitlines()
        
        secondaryPools = []
        secondaryPools.append(" ")
        for currPool in pools:
            secondaryPools.append(currPool)
        
        # Get the groups.
        output = CallDeadlineCommand( ["-groups",] )
        groups = output.splitlines()
        
        # DeadlineFRGlobals contains initial values for the submission dialog. These can be modified
        # by an external sanity check script.
        DeadlineGlobals.initDepartment = ""
        DeadlineGlobals.initPool = "none"
        DeadlineGlobals.initGroup = "none"
        DeadlineGlobals.initPriority = 50
        DeadlineGlobals.initTaskTimeout = 0
        DeadlineGlobals.initIsBlacklist = False
        DeadlineGlobals.initMachineList = ""
        DeadlineGlobals.initLimitGroups = ""
        DeadlineGlobals.initIsInterruptible = False
        
        DeadlineGlobals.initHostName = ""
        DeadlineGlobals.initPort = 5560
        DeadlineGlobals.initMachineCount = 10
        DeadlineGlobals.initWorkerCount = 2
        DeadlineGlobals.initWorkerMem = 1024
        DeadlineGlobals.initWorkerThreads = 1
        
        dialog = ReserveFrameServerSlavesDialog(maximumPriority, pools, secondaryPools, groups )
        LoadStickySettings(dialog)
        
        dialog.department.setValue( DeadlineGlobals.initDepartment )
        dialog.pool.setValue( DeadlineGlobals.initPool )
        dialog.group.setValue( DeadlineGlobals.initGroup )
        dialog.priority.setValue( DeadlineGlobals.initPriority )
        dialog.taskTimeout.setValue( DeadlineGlobals.initTaskTimeout )
        dialog.isBlacklist.setValue( DeadlineGlobals.initIsBlacklist )
        dialog.machineList.setValue( DeadlineGlobals.initMachineList )
        dialog.limitGroups.setValue( DeadlineGlobals.initLimitGroups )
        dialog.isInterruptible.setValue( DeadlineGlobals.initIsInterruptible )
        
        dialog.hostName.setValue( DeadlineGlobals.initHostName )
        dialog.port.setValue( DeadlineGlobals.initPort )
        dialog.workers.setValue( DeadlineGlobals.initWorkerCount )
        dialog.workerThreads.setValue( DeadlineGlobals.initWorkerThreads )
        dialog.workerMem.setValue ( DeadlineGlobals.initWorkerMem )
        dialog.machines.setValue( DeadlineGlobals.initMachineCount )

        # Get the fqdn or hostname if not on resolvable domain/dns AND sticky setting is ""
        if dialog.hostName.value() == "":
            dialog.hostName.setValue( socket.getfqdn() )
        
        success = False
        while not success:
            success = dialog.ShowDialog()
            if not success:
                return
        
    except:
        nuke.executeInMainThread( showNukeMessage, traceback.format_exc() )