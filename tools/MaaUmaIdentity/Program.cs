using System.Text;

namespace MaaUma.Identity;

internal static class Program
{
    public static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--device-identity") return WindowsDeviceIdentity.Run();
        Console.OutputEncoding = new UTF8Encoding(false);
        Console.WriteLine("{\"ok\":false,\"code\":\"device_identity_unavailable\"}");
        return 2;
    }
}
