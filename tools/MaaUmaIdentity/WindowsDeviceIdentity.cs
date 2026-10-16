using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using Microsoft.Win32;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MaaUma.Identity;

// The private identity and service credentials are scoped to this Windows user.
// The machine digest remains stable across application folders and updates.
internal static class WindowsDeviceIdentity
{
    private const string Domain="MaaUma-Device-v1";
    private static readonly byte[] Entropy=Encoding.UTF8.GetBytes("MaaUma/private-service/windows-user/v1");
    [StructLayout(LayoutKind.Sequential)] private struct Blob {public int Length;public IntPtr Data;}
    [DllImport("crypt32.dll",SetLastError=true,CharSet=CharSet.Unicode)]
    [return:MarshalAs(UnmanagedType.Bool)] private static extern bool CryptProtectData(ref Blob input,string description,ref Blob entropy,IntPtr reserved,IntPtr prompt,int flags,out Blob output);
    [DllImport("crypt32.dll",SetLastError=true,CharSet=CharSet.Unicode)]
    [return:MarshalAs(UnmanagedType.Bool)] private static extern bool CryptUnprotectData(ref Blob input,IntPtr description,ref Blob entropy,IntPtr reserved,IntPtr prompt,int flags,out Blob output);
    [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr memory);

    internal static int Run()
    {
        Console.InputEncoding=Encoding.UTF8;Console.OutputEncoding=new UTF8Encoding(false);
        JObject response;
        try {
            if(!OperatingSystem.IsWindows())throw new IdentityException("device_identity_unavailable");
            var buffer=new char[2*1024*1024+1];var count=Console.In.ReadBlock(buffer,0,buffer.Length);
            if(count==buffer.Length)throw new IdentityException("device_identity_unavailable");
            response=Dispatch(JObject.Parse(new string(buffer,0,count)));
            response["ok"]=true;
        } catch(IdentityException error) {response=new JObject {["ok"]=false,["code"]=error.Message};}
          catch(Exception) {response=new JObject {["ok"]=false,["code"]="device_identity_unavailable"};}
        Console.WriteLine(response.ToString(Formatting.None));
        return (bool?)response["ok"]==true ? 0 : 2;
    }

    private static JObject Dispatch(JObject request)
    {
        var action=(string?)request["action"];
        if(action is not ("identity" or "sign" or "seal" or "unseal"))throw new IdentityException("device_identity_unavailable");
        var deviceId=MachineDigest();
        if(action=="unseal") {
            try {
                var opened=JObject.Parse(Encoding.UTF8.GetString(Dpapi(Convert.FromBase64String((string?)request["encrypted"] ?? ""),false)));
                if((string?)opened["device_id"]!=deviceId)throw new IdentityException("device_changed");
                return new JObject {["payload_base64"]=(string?)opened["payload_base64"]};
            } catch(IdentityException) {throw;}
              catch(Exception) {throw new IdentityException("device_credentials_unreadable");}
        }
        using var key=LoadKey(deviceId);
        if(action=="identity")return new JObject {["device_id"]=deviceId,["public_key"]=Convert.ToBase64String(key.ExportSubjectPublicKeyInfo())};
        if(action=="sign") {
            var message=Convert.FromBase64String((string?)request["message_base64"] ?? "");
            if(message.Length>4096 || !Encoding.UTF8.GetString(message).StartsWith(Domain+"\n",StringComparison.Ordinal))throw new IdentityException("device_identity_unavailable");
            return new JObject {["signature"]=Convert.ToBase64String(key.SignData(message,HashAlgorithmName.SHA256,DSASignatureFormat.IeeeP1363FixedFieldConcatenation))};
        }
        var payload=(string?)request["payload_base64"] ?? "";
        if(Convert.FromBase64String(payload).Length>512*1024)throw new IdentityException("device_credentials_unreadable");
        var plaintext=Encoding.UTF8.GetBytes(new JObject {["device_id"]=deviceId,["payload_base64"]=payload}.ToString(Formatting.None));
        try {return new JObject {["encrypted"]=Convert.ToBase64String(Dpapi(plaintext,true))};}
        finally {CryptographicOperations.ZeroMemory(plaintext);}
    }

    private static string MachineDigest()
    {
        using var machine=RegistryKey.OpenBaseKey(RegistryHive.LocalMachine,RegistryView.Registry64);
        using var cryptography=machine.OpenSubKey(@"SOFTWARE\Microsoft\Cryptography");
        if(cryptography?.GetValue("MachineGuid") is not string machineGuid || !Guid.TryParse(machineGuid,out var parsed))
            throw new IdentityException("device_identity_unavailable");
        return Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(Domain+"\n"+parsed.ToString("D"))));
    }

    private static ECDsa LoadKey(string deviceId)
    {
        var folder=Path.Combine((Environment.GetEnvironmentVariable("LOCALAPPDATA") ?? Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData)),"MaaUma","PrivateService");
        Directory.CreateDirectory(folder);
        if((File.GetAttributes(folder)&FileAttributes.ReparsePoint)!=0)throw new IdentityException("device_key_unreadable");
        var path=Path.Combine(folder,"device-key-v1.bin");
        // Serialize simultaneous activation/update processes without exposing key material.
        using var gate=new Mutex(false,@"Local\MaaUma-device-identity-"+Environment.UserName);
        var held=false;
        try {
            try {held=gate.WaitOne(TimeSpan.FromSeconds(4));}catch(AbandonedMutexException){held=true;}
            if(!held)throw new IdentityException("device_identity_unavailable");
            if(!File.Exists(path)) {
                using var created=ECDsa.Create(ECCurve.NamedCurves.nistP256);
                var privateBytes=created.ExportPkcs8PrivateKey();
                byte[]? raw=null;
                try {
                    raw=Encoding.UTF8.GetBytes(new JObject {["device_id"]=deviceId,["private_key"]=Convert.ToBase64String(privateBytes)}.ToString(Formatting.None));
                    using var stream=new FileStream(path,FileMode.CreateNew,FileAccess.Write,FileShare.None);
                    var protectedBytes=Dpapi(raw,true);stream.Write(protectedBytes);stream.Flush(true);
                }finally {CryptographicOperations.ZeroMemory(privateBytes);if(raw!=null)CryptographicOperations.ZeroMemory(raw);}
            }
            if((File.GetAttributes(path)&FileAttributes.ReparsePoint)!=0)throw new IdentityException("device_key_unreadable");
            byte[]? decrypted=null;byte[]? privateKey=null;
            try {
                decrypted=Dpapi(File.ReadAllBytes(path),false);
                var record=JObject.Parse(Encoding.UTF8.GetString(decrypted));
                if((string?)record["device_id"]!=deviceId)throw new IdentityException("device_changed");
                privateKey=Convert.FromBase64String((string?)record["private_key"] ?? "");
                var key=ECDsa.Create();
                try {key.ImportPkcs8PrivateKey(privateKey,out var consumed);if(consumed!=privateKey.Length || key.KeySize!=256)throw new CryptographicException();return key;}
                catch {key.Dispose();throw;}
            }catch(IdentityException){throw;}
             catch(Exception){throw new IdentityException("device_key_unreadable");}
             finally {if(decrypted!=null)CryptographicOperations.ZeroMemory(decrypted);if(privateKey!=null)CryptographicOperations.ZeroMemory(privateKey);}
        } finally {if(held)gate.ReleaseMutex();}
    }

    private static byte[] Dpapi(byte[] bytes,bool protect)
    {
        var input=new Blob {Length=bytes.Length,Data=Marshal.AllocHGlobal(bytes.Length)};
        var entropy=new Blob {Length=Entropy.Length,Data=Marshal.AllocHGlobal(Entropy.Length)};
        Blob output=default;
        try {
            Marshal.Copy(bytes,0,input.Data,bytes.Length);Marshal.Copy(Entropy,0,entropy.Data,Entropy.Length);
            var ok=protect ? CryptProtectData(ref input,"MaaUma private service",ref entropy,IntPtr.Zero,IntPtr.Zero,1,out output)
                           : CryptUnprotectData(ref input,IntPtr.Zero,ref entropy,IntPtr.Zero,IntPtr.Zero,1,out output);
            if(!ok)throw new CryptographicException();
            var result=new byte[output.Length];Marshal.Copy(output.Data,result,0,result.Length);return result;
        }finally {
            for(var i=0;i<input.Length;i++)Marshal.WriteByte(input.Data,i,0);
            Marshal.FreeHGlobal(input.Data);Marshal.FreeHGlobal(entropy.Data);
            if(output.Data!=IntPtr.Zero){for(var i=0;i<output.Length;i++)Marshal.WriteByte(output.Data,i,0);LocalFree(output.Data);}
        }
    }

    private sealed class IdentityException(string code):Exception(code);
}
