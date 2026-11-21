using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using System.Globalization;
using Newtonsoft.Json.Linq;
namespace MFAAvalonia.Helper;
public sealed record PortableConnection(string AdbPath,int Port)
{
    public string Endpoint=>$"127.0.0.1:{Port}";
    public static PortableConnection Load(string root)
    {
        var path=Path.Combine(root,"portable_config.json");
        if(!File.Exists(path))return new("",16384);
        var value=JObject.Parse(File.ReadAllText(path));
        var port=(int?)value["adb_port"] ?? 16384;
        if(port<1 || port>65535)throw new InvalidOperationException("ADB端口应为1至65535。");
        return new((string?)value["adb_path"] ?? "",port);
    }
    public static PortableConnection Parse(string path,string port)
    {
        if(!int.TryParse(port.Trim(),NumberStyles.None,CultureInfo.InvariantCulture,out var number) || number<1 || number>65535)
            throw new InvalidOperationException("ADB端口应为1至65535的整数。");
        path=Path.GetFullPath(path.Trim());
        if(!File.Exists(path))throw new InvalidOperationException("请选择本机模拟器的adb.exe。");
        return new(path,number);
    }
    public void Save(string root)
    {
        var path=Path.Combine(root,"portable_config.json");
        var value=File.Exists(path) ? JObject.Parse(File.ReadAllText(path)) : new JObject();
        value["adb_path"]=AdbPath;value["adb_port"]=Port;
        if((int?)value["schema_version"] is null or <2 || value["capture_backend"]==null)value["capture_backend"]="auto";
        value["schema_version"]=2;
        var temporary=path+".ui.tmp";File.WriteAllText(temporary,value.ToString());File.Move(temporary,path,true);
    }
    public static bool AllowedEndpoint(string endpoint)=>endpoint.StartsWith("127.0.0.1:",StringComparison.Ordinal)
        && int.TryParse(endpoint[10..],NumberStyles.None,CultureInfo.InvariantCulture,out var port) && port is >0 and <=65535;
}
