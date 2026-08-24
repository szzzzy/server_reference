Set shell = CreateObject("WScript.Shell")
base = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
command = Chr(34) & shell.ExpandEnvironmentStrings("%ComSpec%") & Chr(34) & _
          " /d /c " & Chr(34) & Chr(34) & base & "\start_v4_hidden.cmd" & Chr(34) & Chr(34)
shell.Run command, 0, False
