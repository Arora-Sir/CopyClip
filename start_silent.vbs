' Launch CopyClip with no console window.
' Used by the scheduled task, and handy for a manual background start.
' Logs still go to logs\copyclip.log.

Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)

' pythonw.exe runs without allocating a console. Fall back to python.exe if absent.
pyw = shell.RegRead("HKLM\SOFTWARE\Python\PythonCore\3.14\InstallPath\") & "pythonw.exe"
If Not fso.FileExists(pyw) Then pyw = "pythonw.exe"

shell.Run """" & pyw & """ """ & here & "\copyclip.py""", 0, False
