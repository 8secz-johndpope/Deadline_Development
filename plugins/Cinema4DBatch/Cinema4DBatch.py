import io
import os
import tempfile

from Deadline.Plugins import DeadlinePlugin, PluginType
from Deadline.Scripting import FileUtils, RepositoryUtils, SystemUtils
from FranticX.Net import ListeningSocket, SimpleSocketException, SimpleSocketTimeoutException
from FranticX.Processes import ManagedProcess
from System import DateTime, TimeSpan
from System.Diagnostics import ProcessPriorityClass
from System.IO import File, Path
from System.Text import Encoding, StringBuilder
from System.Text.RegularExpressions import Regex


######################################################################
## This is the function that Deadline calls to get an instance of the
## main DeadlinePlugin class.
######################################################################
def GetDeadlinePlugin():
    return Cinema4DBatchPlugin()

def CleanupDeadlinePlugin( deadlinePlugin ):
    deadlinePlugin.Cleanup()
    
######################################################################
## This is the main DeadlinePlugin class for the Cinema4D plugin.
######################################################################
class Cinema4DBatchPlugin( DeadlinePlugin ):
    MyCinema4DController = None

    def __init__( self ):
        self.InitializeProcessCallback += self.InitializeProcess
        self.StartJobCallback += self.StartJob
        self.RenderTasksCallback += self.RenderTasks
        self.EndJobCallback += self.EndJob
        
    ## Called by Deadline to initialize the process.
    def InitializeProcess( self ):
        # Set the plugin specific settings.
        self.PluginType = PluginType.Advanced

        self.version = self.GetIntegerPluginInfoEntryWithDefault( "Version", 20 )
        
    def Cleanup( self ):
        del self.InitializeProcessCallback
        
    ## Called by Deadline when the job is first loaded.
    def StartJob( self ):
        self.LogInfo( "Start Job called - starting up Cinema 4D Batch plugin" )
        
        self.MyCinema4DController = Cinema4DController(self)
        self.MyCinema4DController.SetRenderExecutable()
        # Initialize the Cinema4D controller.
        self.MyCinema4DController.slaveDirectory = self.GetSlaveDirectory()
        
        # Start Cinema4D.
        self.MyCinema4DController.StartCinema4D()
        
    def RenderTasks( self ):
        self.LogInfo( "Render Tasks called" )
        self.MyCinema4DController.RenderTasks()
        
    def EndJob( self ):
        self.LogInfo( "End Job called - shutting down Cinema 4D Batch plugin" )
                
        if self.MyCinema4DController:
            # End the Cinema4D job (unloads the scene file, etc).
            self.MyCinema4DController.EndCinema4DJob()
        
class Cinema4DController( object ):
    Plugin = None
    ProgramName = "Cinema4DProcess"
    
    Cinema4DSocket = None
    Cinema4DStartupFile = ""
    slaveDirectory = ""
    Cinema4DFilename = ""
    Cinema4DRenderExecutable = ""
    ScriptFilename = ""
    ScriptJob = False
    
    ManagedCinema4DProcessRenderArgument = ""
    ManagedCinema4DProcessStartupDirectory = ""
    ManagedCinema4DProcessRenderExecutable = ""
    AuthenticationToken = ""
    
    LoadCinema4DTimeout = 1000
    ProgressUpdateTimeout = 8000
    
    LocalRendering = False
    NetworkFilePath = ""
    LocalFilePath = ""
    NetworkMPFilePath = ""
    LocalMPFilePath = ""

    FunctionRegex = Regex( "FUNCTION: (.*)" )
    SuccessMessageRegex = Regex( "SUCCESS: (.*)" )
    SuccessNoMessageRegex = Regex( "SUCCESS" )
    CanceledRegex = Regex( "CANCELED" )
    ErrorRegex = Regex( "ERROR: (.*)" )
    
    StdoutRegex = Regex( "STDOUT: (.*)" )
    WarnRegex = Regex( "WARN: (.*)" )
    
    def __init__( self, plugin ):
        self.Plugin = plugin
        self.ProgramName = "Cinema4DProcess"
        
        self.LoadCinema4DTimeout = self.Plugin.GetIntegerConfigEntryWithDefault( "LoadC4DTimeout", 1000 )
        self.ProgressUpdateTimeout = self.Plugin.GetIntegerConfigEntryWithDefault( "ProgressUpdateTimeout", 8000 )
        
        # Create the temp script file.
        self.renderTempDirectory = self.Plugin.CreateTempDirectory( "thread" + str(self.Plugin.GetThreadNumber()) )
        
    def Cleanup( self ):
        pass
        
    ########################################################################
    ## Main functions (to be called from Deadline Entry Functions)
    ########################################################################
    # Reads in the plugin configuration settings and sets up everything in preparation to launch Cinema4D.
    # Also does some checking to ensure a Cinema4D job can be rendered on this machine.
    def SetRenderExecutable( self ):
        version = self.Plugin.version
        if version < 15:
            self.Plugin.FailRender( "Cinema 4D " + str(version) + " is not supported for the Batch plugin, please use the normal Cinema 4D plugin.")

        C4DExe = ""
        C4DExeList = self.Plugin.GetConfigEntry( "C4D_" + str(version) + "_RenderExecutable" )
        build = self.Plugin.GetPluginInfoEntryWithDefault( "Build", "None" ).lower().strip()
        
        if SystemUtils.IsRunningOnWindows():
            if build == "32bit":
                self.Plugin.LogInfo( "Enforcing 32 bit build of Cinema 4D" )
                C4DExe = FileUtils.SearchFileListFor32Bit( C4DExeList )
                if not C4DExe:
                    self.Plugin.LogWarning( "32 bit Cinema 4D " + str(version) + " render executable was not found in the semicolon separated list \"" + C4DExeList + "\".  Checking for any executable that exists instead." )
            elif build == "64bit":
                self.Plugin.LogInfo( "Enforcing 64 bit build of Cinema 4D" )
                C4DExe = FileUtils.SearchFileListFor64Bit( C4DExeList )
                if not C4DExe:
                    self.Plugin.LogWarning( "64 bit Cinema 4D " + str(version) + " render executable was not found in the semicolon separated list \"" + C4DExeList + "\".  Checking for any executable that exists instead." )
            
        if not C4DExe:
            self.Plugin.LogInfo( "Not enforcing a build of Cinema 4D" )
            C4DExe = FileUtils.SearchFileList( C4DExeList )
            if not C4DExe:
                self.Plugin.FailRender( "Cinema 4D " + str(version) + " render executable was not found in the semicolon separated list \"" + C4DExeList + "\". The path to the render executable can be configured from the Plugin Configuration in the Deadline Monitor." )
        
        self.Cinema4DRenderExecutable = C4DExe
    
    def ProcessPath( self, filepath ):
        if SystemUtils.IsRunningOnWindows():
            filepath = filepath.replace("/","\\")
            if filepath.startswith( "\\" ) and not filepath.startswith( "\\\\" ):
                filepath = "\\" + filepath
        else:
            filepath = filepath.replace("\\","/")
        return filepath

    def setDirectoryToLoadPlugin( self ):
        """
        Sets up the environment to tell Cinema 4D where to load DeadlineConnect.pyp.
        R19 and before use C4D_PLUGINS_DIR, R20 and later use g_additionalModulePath
        :return: None
        """
        # In Cinema 4D R20 the support for the C4D_PLUGINS_DIR environment variable has
        # been removed. This was done to avoid loading old plugins.
        envVariable = "g_additionalModulePath"
        if self.Plugin.version < 20:
            envVariable = "C4D_PLUGINS_DIR"

        pluginDir = self.Plugin.GetPluginDirectory()
        jobC4dPluginDirs = self.Plugin.GetProcessEnvironmentVariable( envVariable )
        sysC4dPluginDirs = os.getenv( envVariable, "" )

        c4dPluginDirs = ""
        if jobC4dPluginDirs:
            c4dPluginDirs = jobC4dPluginDirs
        elif sysC4dPluginDirs:
            c4dPluginDirs = sysC4dPluginDirs

        if c4dPluginDirs:
            pluginDir += ";"
        # Pre-pending our plugin dir due to a bug in R18 & R19 not supporting multiple paths for C4D_PLUGINS_DIR
        c4dPluginDirs = pluginDir + c4dPluginDirs

        self.Plugin.SetProcessEnvironmentVariable( envVariable, c4dPluginDirs )
        self.Plugin.LogInfo( "[%s] set to: %s" % ( envVariable, c4dPluginDirs ) )
        
    def StartCinema4D( self ):
        # Setup the command line parameters, and then start Cinema4D.
        sceneFile = self.Plugin.GetPluginInfoEntryWithDefault( "SceneFile", self.Plugin.GetDataFilename() )
        sceneFile = RepositoryUtils.CheckPathMapping( sceneFile )
        sceneFile = self.ProcessPath( sceneFile )
        
        self.setDirectoryToLoadPlugin()
        
        self.AuthenticationToken = str( DateTime.Now.TimeOfDay.Ticks )
        
        if SystemUtils.IsRunningOnLinux() and self.Plugin.GetBooleanConfigEntryWithDefault( "SetLinuxEnvironment", True ):
            c4dDir = os.path.dirname( self.Cinema4DRenderExecutable )
            ldPath = os.environ.get( "LD_LIBRARY_PATH", "" )
            pyPath = os.environ.get( "PYTHONPATH", "" )
            path = os.environ.get( "PATH", "" )
            
            modLdPath = "%s/../lib64:%s/resource/modules/python/Python.linux64.framework/lib64:%s/resource/modules/embree.module/libs/linux64:%s" % ( c4dDir, c4dDir, c4dDir, ldPath )
            modPyPath = "%s/resource/modules/python/Python.linux64.framework/lib/python2.7:%s/resource/modules/python/Python.linux64.framework/lib64/python2.7/lib-dynload:%s" % ( c4dDir, c4dDir, pyPath )
            modPath = "%s:%s" % ( path, c4dDir )
            
            self.Plugin.LogInfo( "[LD_LIBRARY_PATH] set to %s" % modLdPath )
            self.Plugin.LogInfo( "[PYTHONPATH] set to %s" % modPyPath )
            self.Plugin.LogInfo( "[PATH] set to %s" % modPath )
            self.Plugin.SetProcessEnvironmentVariable( "LD_LIBRARY_PATH", modLdPath )
            self.Plugin.SetProcessEnvironmentVariable( "PYTHONPATH", modPyPath )
            self.Plugin.SetProcessEnvironmentVariable( "PATH", modPath )

        # Initialize the listening socket.
        self.Cinema4DSocket = ListeningSocket()
        self.Cinema4DSocket.StartListening( 0, True, True, 10 )
        if not self.Cinema4DSocket.IsListening:
            self.Plugin.FailRender( "Failed to open a port for listening to Cinema 4D" )
        else:
            self.Plugin.LogInfo( "Cinema 4D socket connection port: %d" % self.Cinema4DSocket.Port )
        
        parameters = [ "-nogui" ]

        # NOTE: Negate the "No OpenGL" plugin info option from the monitor submitter to make the logic below
        # easier to read
        self.loadOpenGL = not self.Plugin.GetBooleanPluginInfoEntryWithDefault( "NoOpenGL", False )

        # If the integrated submitter has specified a renderer other than Hardware OpenGL
        # we can skip loading OpenGL
        renderer = self.Plugin.GetPluginInfoEntryWithDefault( "Renderer", "" )
        if self.loadOpenGL and renderer not in ( "", "ogl_hardware" ):
            self.loadOpenGL = False

        if not self.loadOpenGL:
            parameters.append( "-noopengl" )

        threads = self.GetNumThreads()
        if threads > 0:
            parameters.append( "-threads %s" % str( threads ) )

        selectedGPUs = self.GetGpuOverrides()
        if selectedGPUs:
            for gpu in selectedGPUs:
                parameters.append( "-redshift-gpu %s" % gpu )

        redshiftLogVerbosity = self.Plugin.GetConfigEntryWithDefault( "RedshiftLogging", "Debug" )
        if redshiftLogVerbosity != "None":
            parameters.append( "-redshift-log-console %s" % redshiftLogVerbosity )
        
        self.importTestFile = os.path.join( self.Plugin.CreateTempDirectory( "importTest" ), "importCheck.txt")
        parameters.append( '"-DeadlineConnect %s %s \'%s\'"' % ( self.Cinema4DSocket.Port, self.AuthenticationToken, self.importTestFile ) )

        parameterString = " ".join(parameters)
        self.Plugin.LogInfo( "Parameters: %s" % parameterString )
        self.LaunchCinema4D( self.Cinema4DRenderExecutable, parameterString, os.path.dirname( self.Cinema4DRenderExecutable ) )
        self.WaitForConnection( "Cinema 4D startup" )
        self.Plugin.LogInfo( "Connected to Cinema 4D" )
        
        verbose = self.Plugin.GetBooleanConfigEntryWithDefault( "Verbose", False )
        
        self.Cinema4DSocket.Send( "Verbose:" + str( verbose ) )
        self.Plugin.LogInfo( self.PollUntilComplete( False ) )

        self.Cinema4DSocket.Send( "DeadlineStartup:" + sceneFile )
        self.Plugin.LogInfo( self.PollUntilComplete( False ) )

        self.SendPathMapping()
    
    def GetNumThreads( self ):
        """
        Returns the number of threads we want to use based off the number of threads specified in the job and the Slave's CPU Affinity
        :return: The number of threads
        """
        threads = self.Plugin.GetIntegerPluginInfoEntryWithDefault( "Threads", 0 )
        # OverrideCpuAffinity - Returns whether the Slave has its CPU affinity override enabled
        if self.Plugin.OverrideCpuAffinity():
            #CPUAffinity - returns a list containing the indices of all CPUs the slave has in its affinity
            affinity = len( self.Plugin.CpuAffinity() )
            if threads == 0:
                threads = affinity
            else:
                threads = min( affinity, threads )
                
        return threads   
    
    def GetGpuOverrides( self ):
        # If the number of gpus per task is set, then need to calculate the gpus to use.
        gpusPerTask = self.Plugin.GetIntegerPluginInfoEntryWithDefault( "GPUsPerTask", 0 )
        gpusSelectDevices = self.Plugin.GetPluginInfoEntryWithDefault( "GPUsSelectDevices", "" )
        resultGPUs = []

        if self.Plugin.OverrideGpuAffinity():
            overrideGPUs = self.Plugin.GpuAffinity()
            if gpusPerTask == 0 and gpusSelectDevices != "":
                gpus = gpusSelectDevices.split( "," )
                notFoundGPUs = []
                for gpu in gpus:
                    if int( gpu ) in overrideGPUs:
                        resultGPUs.append( gpu )
                    else:
                        notFoundGPUs.append( gpu )
                
                if len( notFoundGPUs ) > 0:
                    self.Plugin.LogWarning( "The Slave is overriding its GPU affinity and the following GPUs do not match the Slaves affinity so they will not be used: " + ",".join(notFoundGPUs) )
                if len( resultGPUs ) == 0:
                    self.Plugin.FailRender( "The Slave does not have affinity for any of the GPUs specified in the job." )
            elif gpusPerTask > 0:
                if gpusPerTask > len( overrideGPUs ):
                    self.Plugin.LogWarning( "The Slave is overriding its GPU affinity and the Slave only has affinity for " + str( len( overrideGPUs ) ) + " Slaves of the " + str( gpusPerTask ) + " requested." )
                    resultGPUs =  overrideGPUs
                else:
                    resultGPUs = list( overrideGPUs )[:gpusPerTask]
            else:
                resultGPUs = overrideGPUs
        elif gpusPerTask == 0 and gpusSelectDevices != "":
            resultGPUs = gpusSelectDevices.split( "," )

        elif gpusPerTask > 0:
            gpuList = []
            for i in range( ( self.Plugin.GetThreadNumber() * gpusPerTask ), ( self.Plugin.GetThreadNumber() * gpusPerTask ) + gpusPerTask ):
                gpuList.append( str( i ) )
            resultGPUs = gpuList
        
        resultGPUs = list( resultGPUs )
        
        return resultGPUs
        
    def SendPathMapping( self ):
        pathMappings = RepositoryUtils.GetPathMappings()
        if len( pathMappings ) > 0:
            args = [ self.Plugin.CreateTempDirectory( "pathmapping" ) ]
            texPathFile = self.createTexturePathFile(  )
            if texPathFile:
                args.append( texPathFile )
            
            self.Cinema4DSocket.Send( "Pathmap:" + ";".join(args) )
            self.Plugin.LogInfo( self.PollUntilComplete( False ) )
    
    def createTexturePathFile( self ):
        texPathFileName = None
        if self.Plugin.GetBooleanPluginInfoEntryWithDefault( "HasTexturePaths", False ):
            texPathFileName = os.path.join( self.renderTempDirectory, "texturePaths.txt" )
            with io.open( texPathFileName, mode="w", encoding="utf-8" ) as texPathFile:
                if self.Plugin.version >= 20:
                    index = 0
                    while True:
                        searchPath = self.Plugin.GetPluginInfoEntryWithDefault( "TexturePath%s" % index, "" )
                        if not searchPath:
                            break
                        texPathFile.write( RepositoryUtils.CheckPathMapping( searchPath ) + "\n" )
                        index += 1
                else:
                    for index in range(10):
                        texPath = self.Plugin.GetPluginInfoEntryWithDefault( "TexturePath%s" % index, "" )
                        texPathFile.write( RepositoryUtils.CheckPathMapping( texPath )+ "\n" )
        
        return texPathFileName
    
    def writeSetTakeData( self, scriptBuilder, activeTake ):
        scriptBuilder.AppendLine( )
        scriptBuilder.AppendLine( "# Iterate through objects in take (op)" )
        scriptBuilder.AppendLine( "def GetNextObject( op ):" )
        scriptBuilder.AppendLine( "    if op==None:" )
        scriptBuilder.AppendLine( "        return None" )
        scriptBuilder.AppendLine( "    if op.GetDown():" )
        scriptBuilder.AppendLine( "        return op.GetDown()" )
        scriptBuilder.AppendLine( "    while not op.GetNext() and op.GetUp():" )
        scriptBuilder.AppendLine( "        op = op.GetUp()" )
        scriptBuilder.AppendLine( "    return op.GetNext()" )                    
        
        scriptBuilder.AppendLine( )
        scriptBuilder.AppendLine( "from c4d.modules import takesystem" )
        scriptBuilder.AppendLine( "takeData = deadlineDoc.GetTakeData()" )  
        scriptBuilder.AppendLine( "mainTake = takeData.GetMainTake()" )
        scriptBuilder.AppendLine( "take = GetNextObject( mainTake )" )
        scriptBuilder.AppendLine( )
        scriptBuilder.AppendLine( "while take is not None:" )
        scriptBuilder.AppendLine( "    if take.GetName() == \""+activeTake+"\":" )
        scriptBuilder.AppendLine( "        takeData.SetCurrentTake(take)" )
        scriptBuilder.AppendLine( "        break" )
        scriptBuilder.AppendLine( "    take = GetNextObject( take )" )
        scriptBuilder.AppendLine( )
        scriptBuilder.AppendLine( "if take is not None:" )
        scriptBuilder.AppendLine( "    effectiveRenderData = take.GetEffectiveRenderData( takeData )" )
        scriptBuilder.AppendLine( "    if effectiveRenderData is not None:" )
        scriptBuilder.AppendLine( "        rd = effectiveRenderData[ 0 ]" )
    
    def RenderTasks( self ):
        self.Plugin.LogInfo("Pre Build Script")
        renderer = self.Plugin.GetPluginInfoEntryWithDefault( "Renderer", "" )
        exportJob = "Export" in renderer
        
        self.ScriptJob = self.Plugin.GetBooleanPluginInfoEntryWithDefault( "ScriptJob", False )
        
        if self.ScriptJob:
            self.Plugin.LogInfo( "This is a Python Script Job" )

            self.ScriptFilename = self.Plugin.GetPluginInfoEntryWithDefault( "ScriptFilename", "" )

            if not os.path.isabs( self.ScriptFilename ):
                self.ScriptFilename = os.path.join( self.Plugin.GetJobsDataDirectory(), self.ScriptFilename )
            else:
                self.ScriptFilename = RepositoryUtils.CheckPathMapping( self.ScriptFilename )
                self.ScriptFilename = self.ProcessPath( self.ScriptFilename )

            if not os.path.isfile( self.ScriptFilename ):
                self.Plugin.FailRender( "Python Script File is missing: %s" % self.ScriptFilename )

        else:
            scriptBuilder = StringBuilder()
            
            if not exportJob:
                self.RegionRendering = self.Plugin.GetBooleanPluginInfoEntryWithDefault( "RegionRendering", False )
                self.SingleFrameRegionJob = self.Plugin.IsTileJob()
                self.SingleFrameRegionFrame = str(self.Plugin.GetStartFrame())
                self.SingleFrameRegionIndex = self.Plugin.GetCurrentTaskId()
                
                if self.RegionRendering and self.SingleFrameRegionJob:
                    self.StartFrame = str(self.SingleFrameRegionFrame)
                    self.EndFrame = str(self.SingleFrameRegionFrame)
                else:
                    self.StartFrame = str(self.Plugin.GetStartFrame())
                    self.EndFrame = str(self.Plugin.GetEndFrame())
                
                scriptBuilder.AppendLine()
                scriptBuilder.AppendLine( "#!/usr/bin/python" )
                scriptBuilder.AppendLine( "# -*- coding: utf-16-le -*-" )
                scriptBuilder.AppendLine( "import c4d" )
                scriptBuilder.AppendLine( "from c4d import bitmaps" )
                scriptBuilder.AppendLine( "from c4d import documents" )
                scriptBuilder.AppendLine()
                scriptBuilder.AppendLine( "deadlineDoc = documents.GetActiveDocument()" )
                scriptBuilder.AppendLine( "rd = deadlineDoc.GetActiveRenderData()" )
                
                activeTake = self.Plugin.GetPluginInfoEntryWithDefault( "Take", "" )
                if not activeTake == "" and self.Plugin.version >= 17  :
                    self.writeSetTakeData( scriptBuilder, activeTake )
                
                scriptBuilder.AppendLine( "fps = int( rd[c4d.RDATA_FRAMERATE] )" )
                scriptBuilder.AppendLine( "rd[c4d.RDATA_FRAMESEQUENCE] = c4d.RDATA_FRAMESEQUENCE_MANUAL" )
                scriptBuilder.AppendLine( "rd[c4d.RDATA_FRAMEFROM]=c4d.BaseTime(" + str(self.StartFrame ) + ", fps)" )
                scriptBuilder.AppendLine( "rd[c4d.RDATA_FRAMETO]=c4d.BaseTime(" + str( self.EndFrame ) + ", fps)" )
                scriptBuilder.AppendLine( "rd[c4d.RDATA_FRAMESTEP]=1")
                
                width = self.Plugin.GetIntegerPluginInfoEntryWithDefault( "Width", 0 )
                height = self.Plugin.GetIntegerPluginInfoEntryWithDefault( "Height", 0 )
                if width > 0 and height > 0:
                    scriptBuilder.AppendLine( )
                    scriptBuilder.AppendLine( "rd[c4d.RDATA_XRES]=" + str( width ) )
                    scriptBuilder.AppendLine( "rd[c4d.RDATA_YRES]=" + str( height ) )
                
                if self.RegionRendering:
                    scriptBuilder.AppendLine( )
                    if renderer == "octane":
                        scriptBuilder.AppendLine( "def GetOctaneVideoPost():")
                        scriptBuilder.AppendLine( "    video_post = rd.GetFirstVideoPost()" )
                        scriptBuilder.AppendLine( "    while video_post and video_post.GetType() != 1029525:" )
                        scriptBuilder.AppendLine( "        video_post = video_post.GetNext()" )
                        scriptBuilder.AppendLine( "    return video_post" )
                        scriptBuilder.AppendLine( "octane_video_post = GetOctaneVideoPost()" )
                        scriptBuilder.AppendLine( "octane_video_post[c4d.VP_RENDERREGION] = True" )
                    else:
                        scriptBuilder.AppendLine( "rd[c4d.RDATA_RENDERREGION] = True" )
                    
                    if self.SingleFrameRegionJob:
                        self.Left = self.Plugin.GetPluginInfoEntryWithDefault( "RegionLeft" + self.SingleFrameRegionIndex, "0" )
                        self.Right = self.Plugin.GetPluginInfoEntryWithDefault( "RegionRight" + self.SingleFrameRegionIndex, "0" )
                        self.Top = self.Plugin.GetPluginInfoEntryWithDefault( "RegionTop" + self.SingleFrameRegionIndex, "0" )
                        self.Bottom = self.Plugin.GetPluginInfoEntryWithDefault( "RegionBottom" + self.SingleFrameRegionIndex, "0" )
                    else:
                        self.Left = self.Plugin.GetPluginInfoEntryWithDefault( "RegionLeft", "0" ).strip()
                        self.Right = self.Plugin.GetPluginInfoEntryWithDefault( "RegionRight", "0" ).strip()
                        self.Top = self.Plugin.GetPluginInfoEntryWithDefault( "RegionTop", "0" ).strip()
                        self.Bottom = self.Plugin.GetPluginInfoEntryWithDefault( "RegionBottom", "0" ).strip()
                    
                    if renderer == "octane":
                        scriptBuilder.AppendLine( "octane_video_post[c4d.VP_REGION_X1] = %s" % self.Left )
                        scriptBuilder.AppendLine( "octane_video_post[c4d.VP_REGION_Y1] = %s" % self.Top )
                        scriptBuilder.AppendLine( "octane_video_post[c4d.VP_REGION_X2] = %s" % self.Right )
                        scriptBuilder.AppendLine( "octane_video_post[c4d.VP_REGION_Y2] = %s" % self.Bottom )
                    else:
                        scriptBuilder.AppendLine( "rd[c4d.RDATA_RENDERREGION_LEFT] = %s" % self.Left  )
                        scriptBuilder.AppendLine( "rd[c4d.RDATA_RENDERREGION_TOP] = %s" % self.Top )
                        scriptBuilder.AppendLine( "rd[c4d.RDATA_RENDERREGION_RIGHT] = %s" % self.Right )
                        scriptBuilder.AppendLine( "rd[c4d.RDATA_RENDERREGION_BOTTOM] = %s" % self.Bottom )
                                    
                self.LocalRendering = self.Plugin.GetBooleanPluginInfoEntryWithDefault( "LocalRendering", False )
                # Build the output filename from the path and prefix
                filepath = self.Plugin.GetPluginInfoEntryWithDefault( "FilePath", "" ).strip()
                filepath = RepositoryUtils.CheckPathMapping( filepath )
                if filepath:
                    filepath = self.ProcessPath( filepath )
                    
                    if self.LocalRendering:
                        self.NetworkFilePath, postTokens = self.SplitTokens( filepath )
                        self.ValidateFilepath( self.NetworkFilePath )
                        
                        filepath = self.Plugin.CreateTempDirectory( "c4dOutput" )
                        filepath = self.ProcessPath( filepath )
                        
                        self.LocalFilePath = filepath
                        self.ValidateFilepath( self.LocalFilePath )
                        
                        filepath = os.path.join(filepath, postTokens)
                        filepath = self.ProcessPath( filepath )
                        
                        self.Plugin.LogInfo( "Rendering main output to local drive, will copy files and folders to final location after render is complete")
                    else:
                        pathBeforeTokens, _ = self.SplitTokens( filepath )
                        self.ValidateFilepath( pathBeforeTokens )

                        self.Plugin.LogInfo( "Rendering main output to network drive" )
                        
                    fileprefix = ""
                    if self.RegionRendering and self.SingleFrameRegionJob:
                        fileprefix = self.Plugin.GetPluginInfoEntryWithDefault( ( "RegionPrefix%s" % self.SingleFrameRegionIndex ), "" ).strip()
                    else:
                        fileprefix = self.Plugin.GetPluginInfoEntryWithDefault( "FilePrefix", "" ).strip()
                    scriptBuilder.AppendLine()
                    outputPath = os.path.join( filepath, fileprefix )
                    outputPath = outputPath.replace( "\\", "\\\\" ) # Escape the backslashes in the path

                    #Pre Path - Save Options Debugging
                    self.Plugin.LogInfo("Pre checking Path - Save option before set params")
                    scriptBuilder.AppendLine("print rd[c4d.RDATA_MULTIPASS_SAVEIMAGE]")
                    scriptBuilder.AppendLine("print rd[c4d.RDATA_SAVEIMAGE]")
                    scriptBuilder.AppendLine("print rd[c4d.RDATA_PATH]")

                    scriptBuilder.AppendLine( "rd[c4d.RDATA_MULTIPASS_SAVEIMAGE]= True")
                    scriptBuilder.AppendLine( "rd[c4d.RDATA_SAVEIMAGE]= True")
                    scriptBuilder.AppendLine( "rd[c4d.RDATA_PATH]=\"" + outputPath + "\"" )

                    # Post Path - Save Options Debugging
                    self.Plugin.LogInfo("Post checking Path - Save option ")
                    scriptBuilder.AppendLine("print rd[c4d.RDATA_MULTIPASS_SAVEIMAGE]")
                    scriptBuilder.AppendLine("print rd[c4d.RDATA_SAVEIMAGE]")
                    scriptBuilder.AppendLine("print rd[c4d.RDATA_PATH]")
                
                # Build the multipass output filename from the path and prefix
                multifilepath = self.Plugin.GetPluginInfoEntryWithDefault( "MultiFilePath", "" ).strip()
                multifilepath = RepositoryUtils.CheckPathMapping( multifilepath )
                if multifilepath:
                    multifilepath = self.ProcessPath( multifilepath )

                    if self.LocalRendering:
                        self.NetworkMPFilePath, postTokens = self.SplitTokens( multifilepath )
                        self.ValidateFilepath( self.NetworkMPFilePath )
                        
                        multifilepath = self.Plugin.CreateTempDirectory( "c4dOutputMP" )
                        multifilepath = self.ProcessPath( multifilepath )
                        
                        self.LocalMPFilePath = multifilepath
                        self.ValidateFilepath( self.LocalMPFilePath )
                        
                        multifilepath = os.path.join(multifilepath, postTokens)
                        multifilepath = self.ProcessPath( multifilepath )
                        
                        self.Plugin.LogInfo( "Rendering multipass output to local drive, will copy files and folders to final location after render is complete" )
                    else:
                        pathBeforeTokens, _ = self.SplitTokens( multifilepath )
                        self.ValidateFilepath( pathBeforeTokens )

                        self.Plugin.LogInfo( "Rendering multipass output to network drive" )
                    
                    multifileprefix = ""
                    if self.RegionRendering and self.SingleFrameRegionJob:
                        multifileprefix = self.Plugin.GetPluginInfoEntryWithDefault( ( "MultiFileRegionPrefix%s" % self.SingleFrameRegionIndex ), "" ).strip()
                    else:
                        multifileprefix = self.Plugin.GetPluginInfoEntryWithDefault( "MultiFilePrefix", "" ).strip()
                    
                    scriptBuilder.AppendLine( )
                    outputMultiFilePath = os.path.join( multifilepath, multifileprefix )
                    outputMultiFilePath = outputMultiFilePath.replace( "\\", "\\\\" )
                    
                    scriptBuilder.AppendLine( "rd[c4d.RDATA_MULTIPASS_FILENAME]=\"" + outputMultiFilePath + "\"" )
                    
                scriptBuilder.AppendLine( )
                scriptBuilder.AppendLine( "bmp = bitmaps.MultipassBitmap(rd[c4d.RDATA_XRES],rd[c4d.RDATA_YRES], c4d.COLORMODE_RGB)" )
                scriptBuilder.AppendLine( "results = documents.RenderDocument(deadlineDoc, rd.GetData(), bmp, c4d.RENDERFLAGS_EXTERNAL | c4d.RENDERFLAGS_SHOWERRORS)" )
                scriptBuilder.AppendLine( "if results != c4d.RENDERRESULT_OK:" )
                scriptBuilder.AppendLine( "    resArray = ['Function was successful.', 'Not enough memory.', 'Assets (textures etc.) are missing.', 'Failed to save.', 'User stopped the processing.', 'GI cache is missing.']" )
                scriptBuilder.AppendLine( "    print 'RenderDocument failed with return code '+str(results)+' meaning: ' + ( resArray[results] if len(resArray) > results else 'Unknown Error.')" )
                # This can make the logs look a bit messy, and can sometimes be misleading when an error occurs.
                if self.Plugin.GetBooleanConfigEntryWithDefault( "WriteScriptToLog", False ):
                    self.Plugin.LogInfo( "Script contents:" )
                    self.Plugin.LogInfo( scriptBuilder.ToString().replace( "\r", "" ) )
            else:
                # Common Export Code
                scriptBuilder.AppendLine()
                scriptBuilder.AppendLine( "#!/usr/bin/python" )
                scriptBuilder.AppendLine( "# -*- coding: utf-16-le -*-" )
                scriptBuilder.AppendLine( "import c4d" )
                scriptBuilder.AppendLine( "from c4d import documents")
                scriptBuilder.AppendLine( "scene = documents.GetActiveDocument()" )

                activeTake = self.Plugin.GetPluginInfoEntryWithDefault( "Take", "" )
                if not activeTake == "" and self.Plugin.version >= 17  :
                    scriptBuilder.AppendLine( "from c4d.modules import takesystem" )
                    scriptBuilder.AppendLine( "takeData = scene.GetTakeData()" )
                    scriptBuilder.AppendLine( "mainTake = takeData.GetMainTake()" )
                    scriptBuilder.AppendLine( "take = mainTake.GetDown()" )
                    scriptBuilder.AppendLine()
                    scriptBuilder.AppendLine( "while take is not None:" )
                    scriptBuilder.AppendLine( "    if take.GetName() == \"" + activeTake + "\":" )
                    scriptBuilder.AppendLine( "        takeData.SetCurrentTake(take)" )
                    scriptBuilder.AppendLine( "        break" )
                    scriptBuilder.AppendLine( "    take = take.GetNext()" )

                if renderer == "ArnoldExport":
                    self.Plugin.LogInfo( "Exporting to Arnold" )

                    scriptBuilder.AppendLine()
                    scriptBuilder.AppendLine( "ARNOLD_ASS_EXPORT = 1029993" )
                    scriptBuilder.AppendLine( "options = c4d.BaseContainer()" )
                    scriptBuilder.AppendLine( "options.SetInt32( 6, %s )" % self.Plugin.GetStartFrame() )
                    scriptBuilder.AppendLine( "options.SetInt32( 7, %s )" % self.Plugin.GetEndFrame() )

                    assFile = self.ProcessPath( self.Plugin.GetPluginInfoEntryWithDefault( "ExportFile", "" ) )
                    assFile = assFile.replace( "\\", "\\\\" ) # Escape the backslashes in the path
                    if assFile != "":
                        self.ValidateFilepath( os.path.dirname( assFile ) )
                        scriptBuilder.AppendLine( "options.SetFilename( 0, '%s' )" % assFile )

                    scriptBuilder.AppendLine( "scene.GetSettingsInstance( c4d.DOCUMENTSETTINGS_DOCUMENT ).SetContainer( ARNOLD_ASS_EXPORT, options )" )
                    scriptBuilder.AppendLine( "c4d.CallCommand( ARNOLD_ASS_EXPORT )" )

                elif renderer == "RedshiftExport":
                    self.Plugin.LogInfo( "Exporting to Redshift" )

                    scriptBuilder.AppendLine()
                    scriptBuilder.AppendLine( "REDSHIFT_EXPORT_PLUGIN_ID = 1038650" )
                    scriptBuilder.AppendLine( "plug = c4d.plugins.FindPlugin( REDSHIFT_EXPORT_PLUGIN_ID, c4d.PLUGINTYPE_SCENESAVER )" )
                    scriptBuilder.AppendLine( "op = {}" )
                    scriptBuilder.AppendLine( "plug.Message( c4d.MSG_RETRIEVEPRIVATEDATA, op )" )
                    scriptBuilder.AppendLine( "imexporter = op[ \"imexporter\" ]" )
                    scriptBuilder.AppendLine( "imexporter[ c4d.REDSHIFT_PROXYEXPORT_AUTOPROXY_CREATE ] = False" )
                    scriptBuilder.AppendLine( "imexporter[ c4d.REDSHIFT_PROXYEXPORT_ANIMATION_RANGE ] = c4d.REDSHIFT_PROXYEXPORT_ANIMATION_RANGE_MANUAL" )
                    scriptBuilder.AppendLine( "imexporter[ c4d.REDSHIFT_PROXYEXPORT_ANIMATION_FRAME_START ] = %s" % self.Plugin.GetStartFrame() )
                    scriptBuilder.AppendLine( "imexporter[ c4d.REDSHIFT_PROXYEXPORT_ANIMATION_FRAME_END ] = %s" % self.Plugin.GetEndFrame() )
                    scriptBuilder.AppendLine( "imexporter[ c4d.REDSHIFT_PROXYEXPORT_ANIMATION_FRAME_STEP ] = 1" )

                    rsFile = self.ProcessPath( self.Plugin.GetPluginInfoEntryWithDefault( "ExportFile", "" ) )
                    rsFile = rsFile.replace( "\\", "\\\\" ) # Escape the backslashes in the path
                    rsFile = rsFile.replace("#","") # Redshift automatically adds the frame numbers.
                    if rsFile != "":
                        self.ValidateFilepath( os.path.dirname( rsFile ) )
                        scriptBuilder.AppendLine( "documents.SaveDocument(scene, \"%s\", c4d.SAVEDOCUMENTFLAGS_0, REDSHIFT_EXPORT_PLUGIN_ID)" % rsFile )
                        scriptBuilder.AppendLine( "print 'Exported: %s'" % rsFile )
                    else:
                        self.Plugin.FailRender( "Failed to export Redshift Scene - No output file name set." )

            self.ScriptFilename = os.path.join( self.renderTempDirectory, "c4d_Batch_Script.py" )
            File.WriteAllText( self.ScriptFilename, scriptBuilder.ToString(), Encoding.UTF8 )
            self.Plugin.LogInfo( "" )
            if SystemUtils.IsRunningOnMac():
                os.chmod( self.ScriptFilename, os.stat( Path.GetTempFileName() ).st_mode )
        
        self.Cinema4DSocket.Send( "RunScript:" + self.ScriptFilename )
        self.Plugin.LogInfo( self.PollUntilComplete( False ) )
        self.Plugin.FlushMonitoredManagedProcessStdout( self.ProgramName )

        if self.LocalRendering:
            if self.NetworkFilePath != "":
                self.Plugin.LogInfo( "Moving main output files and folders from " + self.LocalFilePath + " to " + self.NetworkFilePath )
                self.Plugin.VerifyAndMoveDirectory( self.LocalFilePath, self.NetworkFilePath, False, -1 )
            if self.NetworkMPFilePath != "":
                self.Plugin.LogInfo( "Moving multipass output files and folders from " + self.LocalMPFilePath + " to " + self.NetworkMPFilePath )
                self.Plugin.VerifyAndMoveDirectory( self.LocalMPFilePath, self.NetworkMPFilePath, False, -1 )

        self.Plugin.LogInfo( "Finished Cinema 4D Task" )

    def SplitTokens( self, filePath ):
        if not "$" in filePath:
            return filePath, ""
            
        preTokenPath = ""
        postTokenPath = ""
        
        tokenIndex = -1
        tokenParts = filePath.replace("\\","/").split("/")
        for i, pathPart in enumerate(tokenParts):
            if "$" in pathPart:
                tokenIndex = i
                break
        
        preTokenPath = "/".join( tokenParts[:tokenIndex] )
        postTokenPath = "/".join( tokenParts[tokenIndex:] )
        
        return preTokenPath, postTokenPath

    def ValidateFilepath( self, directory ):
        self.Plugin.LogInfo( "Validating the path: '%s'" % directory )

        if not os.path.exists( directory ):
            try:
                os.makedirs( directory )
            except:
                self.Plugin.FailRender( "Failed to create path: '%s'" % directory )

        # Test to see if we have permission to create a file
        try:
            # TemporaryFile deletes the "file" when it closes, we only care that it can be created
            with tempfile.TemporaryFile( dir=directory ) as tempFile:
                pass
        except:
            self.Plugin.FailRender( "Failed to create test file in directory: '%s'" % directory )

    # This tells Cinema4D to unload the current scene file.
    def EndCinema4DJob( self ):
        if not self.Plugin.MonitoredManagedProcessIsRunning( self.ProgramName ):
            self.Plugin.LogWarning( "Cinema 4D.exe was shut down before the proper shut down sequence" )
        else:
            response = ""
            
            # If an error occurs while sending EndJob, set the response so that we don't enter the while loop below.
            try:
                self.Plugin.LogInfo("Sending End Job")
                self.Cinema4DSocket.Send( "EndJob" )
            except Exception as e:
                response = ( "ERROR: Error sending EndJob command: %s" % e.Message )
            
            countdown = 5000
            while countdown > 0 and not response:
                try:
                    countdown = countdown - 100
                    response = self.Cinema4DSocket.Receive( 100 )
                    
                    # If this is a STDOUT message, print it out and reset 'response' so that we keep looping
                    match = self.StdoutRegex.Match( response )
                    if match.Success:
                        self.Plugin.LogInfo( match.Groups[ 1 ].Value )
                        response = ""
                    
                    # If this is a WARN message, print it out and reset 'response' so that we keep looping
                    match = self.WarnRegex.Match( response )
                    if match.Success:
                        self.Plugin.LogWarning( match.Groups[ 1 ].Value )
                        response = ""
                    
                except Exception as e:
                    if not isinstance( e, SimpleSocketTimeoutException ):
                        response = ( "ERROR: Error when waiting for renderer to close: %s" % e.Message )
            
            if not response:
                self.Plugin.LogWarning( "Timed out waiting for the renderer to close." )
                
            if response.startswith( "ERROR: " ):
                self.Plugin.LogWarning( response[7:] )
            
            if not response.startswith( "SUCCESS" ):
                self.Plugin.LogWarning( "Did not receive a success message in response to EndJob: %s" % response )
        
        self.Plugin.ShutdownMonitoredManagedProcess( self.ProcessName )
        
    def PollUntilComplete( self, timeoutEnabled, timeoutOverride=-1 ):
        progressCountdown = (self.ProgressUpdateTimeout if timeoutOverride < 0 else timeoutOverride) * 1000
        
        while progressCountdown > 0 and self.Cinema4DSocket.IsConnected and not self.Plugin.IsCanceled():
            try:
                # Verify that Cinema 4D is still running.
                self.Plugin.VerifyMonitoredManagedProcess( self.ProgramName )
                self.Plugin.FlushMonitoredManagedProcessStdout( self.ProgramName )
                
                # Check for any popup dialogs.
                blockingDialogMessage = self.Plugin.CheckForMonitoredManagedProcessPopups( self.ProgramName )
                if blockingDialogMessage:
                    self.Plugin.FailRender( blockingDialogMessage )
                
                # Only decrement the timeout value if timeouts are enabled
                if timeoutEnabled:
                    progressCountdown = progressCountdown - 500
                
                start = DateTime.Now.Ticks
                while TimeSpan.FromTicks( DateTime.Now.Ticks - start ).Milliseconds < 500:
                    request = self.Cinema4DSocket.Receive( 500 )
                    
                    # We received a request, so reset the progress update timeout.
                    progressCountdown = (self.ProgressUpdateTimeout if timeoutOverride < 0 else timeoutOverride) * 1000
                                        
                    match = self.SuccessMessageRegex.Match( request )
                    if match.Success: # Render finished successfully
                        return match.Groups[ 1 ].Value
                    
                    if self.SuccessNoMessageRegex.IsMatch( request ): # Render finished successfully
                        return ""
                    
                    if self.CanceledRegex.IsMatch( request ): # Render was canceled
                        self.Plugin.FailRender( "Render was canceled" )
                        continue
                    
                    match = self.ErrorRegex.Match( request )
                    if match.Success: # There was an error
                        self.Plugin.FailRender( "%s" % match.Groups[ 1 ].Value )
                        continue
                    
            except Exception as e:
                if isinstance( e, SimpleSocketTimeoutException ):
                    if progressCountdown <= 0:
                        if timeoutOverride < 0:
                            self.Plugin.FailRender( "Timed out waiting for the next progress update. The ProgressUpdateTimeout setting can be modified in the Cinema4D Batch plugin configuration." )
                        else:
                            self.Plugin.FailRender( "Timed out waiting for the next progress update." )
                elif isinstance( e, SimpleSocketException ):
                    self.Plugin.FailRender( "RenderTask: Cinema4D may have crashed (%s)" % e.Message )
                else:
                    self.Plugin.FailRender( "RenderTask: Unexpected exception (%s)" % e.Message )
        
        if self.Plugin.IsCanceled():
            self.Plugin.FailRender( "Render was canceled" )
        
        if not self.Cinema4DSocket.IsConnected:
            self.Plugin.FailRender( "Socket disconnected unexpectedly" )
        
        return "undefined"
        
    def LaunchCinema4D( self, executable, arguments, startupDir ):
        self.ManagedCinema4DProcessRenderExecutable = executable
        self.ManagedCinema4DProcessRenderArgument = arguments
        self.ManagedCinema4DProcessStartupDirectory = startupDir
        
        self.Cinema4DProcess = Cinema4DProcess(self)
        self.Plugin.StartMonitoredManagedProcess( self.ProgramName, self.Cinema4DProcess )
        self.Plugin.VerifyMonitoredManagedProcess( self.ProgramName )
    
    def WaitForConnection( self, errorMessageOperation ):
        startTime = DateTime.Now
        receivedToken = ""
        while  DateTime.Now.Subtract( startTime ).TotalSeconds < self.LoadCinema4DTimeout and not self.Cinema4DSocket.IsConnected and not self.Plugin.IsCanceled():
            try:
                self.Plugin.VerifyMonitoredManagedProcess( self.ProgramName )
                self.Plugin.FlushMonitoredManagedProcessStdout( self.ProgramName )
                
                blockingDialogMessage = self.Plugin.CheckForMonitoredManagedProcessPopups( self.ProgramName )
                if blockingDialogMessage:
                    self.Plugin.FailRender( blockingDialogMessage )
                    
                self.Cinema4DSocket.WaitForConnection( 500, True )

                receivedToken = self.Cinema4DSocket.Receive( 3000 )
                if receivedToken.startswith( "TOKEN:" ):
                    receivedToken = receivedToken[6:]
                else:
                    self.Plugin.LogInfo("Disconnecting: "+receivedToken)
                    self.Cinema4DSocket.Disconnect( False )
                self.Plugin.LogInfo("Received Token: "+receivedToken)
            except Exception as e:
                if os.path.isfile(self.importTestFile):
                    failedImports = []
                    with open( self.importTestFile, "r" ) as importFileHandle:
                        for line in importFileHandle:
                            failedImports.append( line.strip() )
                    self.Plugin.FailRender( "Failed to import the following modules: %s\nPlease ensure that your environment is set correctly or that you are allowing Deadline to set the render environment." % ", ".join(failedImports) )

                if not isinstance( e, SimpleSocketTimeoutException ):
                    self.Plugin.FailRender( "%s: Error getting connection from Cinema4D: %s" % (errorMessageOperation, e.Message) )
            
            if self.Plugin.IsCanceled():
                self.Plugin.FailRender( "%s: Initialization was canceled by Deadline" % errorMessageOperation )
        if not self.Cinema4DSocket.IsConnected:
            if DateTime.Now.Subtract( startTime ).TotalSeconds < self.LoadCinema4DTimeout:
                self.Plugin.FailRender( "%s: Cinema4D exited unexpectedly - check that Cinema4D starts up with no dialog messages" % (errorMessageOperation) )
            else:
                self.Plugin.FailRender( "%s: Timed out waiting for Cinema4D to start - consider increasing the LoadCinema4DTimeout in the Cinema4D plugin configuration" % (errorMessageOperation) )
        
        if receivedToken != self.AuthenticationToken:
            self.Plugin.FailRender( "%s: Did not receive expected token from Cinema4d (got \"%s\") - an unexpected error may have occurred during initialization" % (errorMessageOperation, receivedToken) )
    
######################################################################
## This is the class that starts up the Cinema4D process.
######################################################################
class Cinema4DProcess( ManagedProcess ):
    
    Cinema4DController = None
    
    def __init__( self, cinema4DController ):
        self.Cinema4DController = cinema4DController
        self.InitializeProcessCallback += self.InitializeProcess
        self.RenderExecutableCallback += self.RenderExecutable
        self.RenderArgumentCallback += self.RenderArgument
        self.StartupDirectoryCallback += self.StartupDirectory
    
    def Cleanup( self ):
        for stdoutHandler in self.StdoutHandlers:
            del stdoutHandler.HandleCallback
        
        del self.InitializeProcessCallback
        del self.RenderExecutableCallback
        del self.RenderArgumentCallback
        del self.StartupDirectoryCallback
    
    def InitializeProcess( self ):
        self.ProcessPriority = ProcessPriorityClass.BelowNormal
        self.UseProcessTree = True
        self.SingleFramesOnly = False
        self.StdoutHandling = True
        self.PopupHandling = True
        
        self.FinishedFrameCount = 0
        self.CheckProgress = False
        self.CurrentRenderPhase = ""
        self.currFrame = None
        self.prevFrame = self.Cinema4DController.Plugin.GetStartFrame()
        
        #self.AddStdoutHandlerCallback("Error:.*").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(".*Document not found.*").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(".*Project not found.*").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(".*Error rendering project.*").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(".*Error loading project.*").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(".*Error rendering document.*").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(".*Error loading document.*").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(".*Rendering failed.*").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(".*Asset missing.*").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(".*Asset Error.*").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(".*Invalid License from License Server.*").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(".*Files cannot be written.*").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(".*Enter Registration Data.*").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(".*The output resolution is too high for the selected render engine.*").HandleCallback += self.HandleOutputResolutionError
        self.AddStdoutHandlerCallback(".*Unable to write file.*").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(".*RenderDocument failed with return code.*").HandleCallback += self.HandleStdoutError

        self.AddStdoutHandlerCallback(".*Warning: Unknown arguments: -DeadlineConnect.*").HandleCallback += self.HandlePluginEnvironment
        
        self.AddStdoutHandlerCallback(".*Rendering frame ([0-9]+) at.*").HandleCallback +=  self.HandleStdoutProgress
        self.AddStdoutHandlerCallback(".*Rendering Phase: Setup.*").HandleCallback +=  self.HandleSetupProgress
        self.AddStdoutHandlerCallback(".*Rendering Phase: Main Render.*").HandleCallback +=  self.HandleProgressCheck
        self.AddStdoutHandlerCallback(".*Progress: (\d+)%.*").HandleCallback += self.HandleTaskProgress 
        self.AddStdoutHandlerCallback(".*Rendering successful.*").HandleCallback +=  self.HandleProgress2
        self.AddStdoutHandlerCallback(".*Rendering Phase: Finalize.*").HandleCallback +=  self.HandleFrameProgress

        # Redshift progress handling
        self.AddStdoutHandlerCallback(r"Frame rendering aborted.").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(r"Rendering was internally aborted").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(r'Cannot find procedure "rsPreference"').HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(
            r"Rendering frame \d+ \((\d+)/(\d+)\)").HandleCallback += self.HandleRedshiftNewFrameProgress
        self.AddStdoutHandlerCallback(
            r"Block (\d+)/(\d+) .+ rendered").HandleCallback += self.HandleRedshiftBlockRendered

        self.AddStdoutHandlerCallback( ".*ImportError: No module named site.*" ).HandleCallback += self.HandleNoSite
        self.AddStdoutHandlerCallback( ".*code for hash .* was not found." ).HandleCallback += self.HandleHashNotFound

        # Handle QuickTime popup dialog
        # "QuickTime does not support the current Display Setting.  Please change it and restart this application."
        self.AddPopupHandler( "Unsupported Display", "OK" )
        self.AddPopupHandler( "Nicht.*", "OK" )

        self.AddPopupHandler( ".*Render history settings.*", "OK" )

    def RenderExecutable( self ):
        return self.Cinema4DController.ManagedCinema4DProcessRenderExecutable
    
    def RenderArgument( self ):
        return self.Cinema4DController.ManagedCinema4DProcessRenderArgument
    
    def StartupDirectory( self ):
        return self.Cinema4DController.ManagedCinema4DProcessStartupDirectory

    def HandleNoSite( self ):
        self.Cinema4DController.Plugin.FailRender( "Failed to import the following modules: site\nPlease ensure that your environment is set correctly or that you are allowing Deadline to set the render environment.\nPlease go to the C4D FAQ in the Deadline documentation for more information." )

    def HandleHashNotFound( self ):
        self.Cinema4DController.Plugin.LogInfo( "OpenSSL has not been set up to work properly with C4D Batch, this is a non-blocking issue.\nPlease go to the C4D FAQ in the Deadline documentation for more information." )

    def HandleStdoutProgress( self ):
        startFrame = self.Cinema4DController.Plugin.GetStartFrame()
        endFrame = self.Cinema4DController.Plugin.GetEndFrame()

        currFrame = int( self.GetRegexMatch(1) )
        frameCount = abs( endFrame - startFrame ) + 1
        progress = 100 * ( currFrame - startFrame ) / frameCount

        self.Cinema4DController.Plugin.SetProgress( progress )
        self.Cinema4DController.Plugin.SetStatusMessage( self.GetRegexMatch(0) )
        
    def HandleProgress2( self ):
        self.SetProgress( 100 )
        self.SetStatusMessage( self.GetRegexMatch(0) )

    def HandleOutputResolutionError( self ):
        errorMsg = self.GetRegexMatch( 0 )
        if not self.Cinema4DController.loadOpenGL:
            errorMsg = "This job was configured to not load OpenGL. If you are using the Hardware OpenGL renderer, resubmit without the \"Don't Load OpenGL\" option checked. "
        self.Cinema4DController.Plugin.FailRender( errorMsg )

    def HandleStdoutError( self ):
        self.Cinema4DController.Plugin.FailRender(self.GetRegexMatch(0))

    def HandlePluginEnvironment( self ):
        self.Cinema4DController.Plugin.FailRender( self.GetRegexMatch(0) + "\nC4D was unable to locate DeadlineConnect.pyp. This is a known issue in R18 and R19 for Cinema4DBatch, please go to the C4D FAQ in the Deadline documentation for a workaround." )
        
    def HandleSetupProgress( self ):
        #If frame number is given update the Render status with the current frame
        if self.currFrame is not None:
            self.CurrentRenderPhase = "Frame: "+str(self.currFrame)+",  Rendering Phase: Setup"
        else:
            self.CurrentRenderPhase = "Rendering Phase: Setup"

    def HandleProgressCheck( self ):
        self.CheckProgress = True

        #If frame number is given update the Render status with the current frame
        if self.currFrame != None:
            self.CurrentRenderPhase = "Frame: "+str(self.currFrame)+",  Rendering Phase: Main Render"
        else:
            self.CurrentRenderPhase = "Rendering Phase: Main Render"
            
    def HandleTaskProgress( self ):
        startFrame = self.Cinema4DController.Plugin.GetStartFrame()
        endFrame = self.Cinema4DController.Plugin.GetEndFrame()
        frameCount = abs( endFrame - startFrame ) + 1

        # Sometimes progress is reported as over 100%. We don't know why, but we're handling it here.
        subProgress = 1
        if float(self.GetRegexMatch(1)) <= 100:
            subProgress = float(self.GetRegexMatch(1))/100
        
        if self.currFrame is not None and self.CheckProgress:
            if self.prevFrame + subProgress < self.currFrame:
                self.prevFrame = self.currFrame
                progress = 100 * ( self.currFrame - startFrame ) / frameCount
                self.Cinema4DController.Plugin.SetProgress( progress )
            else:
                progress = int( 100 * float( self.currFrame + subProgress - startFrame ) / float( frameCount ) )
                self.Cinema4DController.Plugin.SetProgress( progress )
        
        #Update the 'Task Render Status' with the progress of each Render Phase
        self.Cinema4DController.Plugin.SetStatusMessage( str(self.CurrentRenderPhase)+" - Progress: "+str(self.GetRegexMatch(1))+"%" )
        
    def HandleFrameProgress( self ):
        self.FinishedFrameCount += 1
        self.CheckProgress = False

        #If frame number is given update the Render status with the current frame
        if self.currFrame is not None:
            self.CurrentRenderPhase = "Frame: "+str(self.currFrame)+",  Rendering Phase: Finalize"
        else:
            self.CurrentRenderPhase = "Rendering Phase: Finalize"
        
        startFrame = self.Cinema4DController.Plugin.GetStartFrame()
        endFrame = self.Cinema4DController.Plugin.GetEndFrame()
        frameCount = abs( endFrame - startFrame ) + 1
        progress = 100 * self.FinishedFrameCount / frameCount

        self.Cinema4DController.Plugin.SetProgress( progress )
        self.Cinema4DController.Plugin.LogInfo( "Task Overall Progress: " + str(progress)+"%")

    def HandleRedshiftNewFrameProgress(self):
        self.FinishedFrameCount = float(self.GetRegexMatch(1)) - 1
        startFrame = self.Cinema4DController.Plugin.GetStartFrame()
        endFrame = self.Cinema4DController.Plugin.GetEndFrame()
        frameCount = abs( endFrame - startFrame ) + 1

        progress = 100 * self.FinishedFrameCount / frameCount
        self.Cinema4DController.Plugin.SetProgress( progress )

    def HandleRedshiftBlockRendered(self):
        startFrame = self.Cinema4DController.Plugin.GetStartFrame()
        endFrame = self.Cinema4DController.Plugin.GetEndFrame()
        frameCount = abs( endFrame - startFrame ) + 1

        completedBlockNumber = float(self.GetRegexMatch(1))
        totalBlockCount = float(self.GetRegexMatch(2))
        finishedFrames = completedBlockNumber / totalBlockCount
        finishedFrames = finishedFrames + self.FinishedFrameCount

        progress = 100 * finishedFrames / frameCount
        self.Cinema4DController.Plugin.SetProgress( progress )