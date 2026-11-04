Option Explicit
Dim sh, fso, root, cmd, e
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root

If HasExe("pyw") Then
  cmd = "pyw -3 """ & root & "\main.py"""
ElseIf HasExe("pythonw") Then
  cmd = "pythonw """ & root & "\main.py"""
ElseIf HasExe("py") Then
  cmd = "py -3 """ & root & "\main.py"""
ElseIf HasExe("python") Then
  cmd = "python """ & root & "\main.py"""
Else
  MsgBox "Python not found. Install Python 3 and add it to PATH.", 16, "FileMate"
  WScript.Quit 1
End If

sh.Run cmd, 0, False
WScript.Quit 0

Function HasExe(exe)
  On Error Resume Next
  e = sh.Run("cmd /c where " & exe & " >nul 2>&1", 0, True)
  HasExe = (e = 0)
  On Error GoTo 0
End Function
