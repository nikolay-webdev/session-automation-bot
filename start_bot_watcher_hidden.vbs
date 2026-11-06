Set WshShell = CreateObject("WScript.Shell")
Set FileSystem = CreateObject("Scripting.FileSystemObject")

projectDir = FileSystem.GetParentFolderName(WScript.ScriptFullName)
watcherPath = FileSystem.BuildPath(projectDir, "watcher.py")

WshShell.CurrentDirectory = projectDir
command = """pythonw.exe"" """ & watcherPath & """"
WshShell.Run command, 0, False
