using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;

namespace MaaUma.Entry;

public static class Launcher
{
    [STAThread]
    public static int Main(string[] args) => Run(AppContext.BaseDirectory, args, ShowError);

    public static ProcessStartInfo CreateStartInfo(string directory, IEnumerable<string> args)
    {
        var home = Path.GetFullPath(directory);
        var info = new ProcessStartInfo(Path.Combine(home, "MFAAvalonia.exe"))
        {
            WorkingDirectory = home,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        foreach (var arg in args) info.ArgumentList.Add(arg);
        var runtime = Path.Combine(home, "dotnet");
        info.Environment["DOTNET_ROOT"] = runtime;
        info.Environment["DOTNET_ROOT_X64"] = runtime;
        info.Environment["DOTNET_MULTILEVEL_LOOKUP"] = "0";
        return info;
    }

    // The actual GUI still runs through the original entry so both entry names
    // share its existing mutex, command pipe, process identity and updater.
    public static int Run(string directory, string[] args, Action<string> reportError)
    {
        try
        {
            var start = CreateStartInfo(directory, args);
            if (!File.Exists(start.FileName))
            {
                reportError("找不到 MaaUma 面板主程序。\n请将整个压缩包解压到同一个文件夹，再打开 MaaUma.exe。\n缺少文件：MFAAvalonia.exe");
                return 1;
            }
            using var child = Process.Start(start);
            if (child is null) throw new InvalidOperationException();
            // Do not wait, terminate, or adopt the GUI process. Its normal close
            // and update/restart flows remain responsible for its lifetime.
            return 0;
        }
        catch (Win32Exception error)
        {
            reportError($"MaaUma 未能启动（系统错误 {error.NativeErrorCode}）。\n请确认文件已完整解压，且未被安全软件隔离，然后重新打开 MaaUma.exe。");
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or InvalidOperationException or ArgumentException)
        {
            reportError("MaaUma 未能启动。\n请确认程序文件完整，并将整个压缩包解压到可访问的文件夹后重试。");
        }
        return 1;
    }

    private static void ShowError(string message) =>
        MessageBoxW(IntPtr.Zero, message, "MaaUma 启动提示", 0x10);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern int MessageBoxW(IntPtr owner, string message, string caption, uint type);
}
