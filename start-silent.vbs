Set WshShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")
strDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
strPy  = strDir & "\python\pythonw.exe"
strMain = strDir & "\main.py"
WshShell.CurrentDirectory = strDir
WshShell.Run """" & strPy & """ """ & strMain & """", 0, False
