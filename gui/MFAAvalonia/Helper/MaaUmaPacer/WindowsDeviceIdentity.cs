using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using Microsoft.Win32;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace UmaCutPanel;

// The private identity and service credentials are scoped to this Windows user.
// The machine digest remains stable across application folders and updates.
internal static class WindowsDeviceIdentity
{
    private const string Domain="MaaUma-Device-v1";
    private static readonly byte[] Entropy=Encoding.UTF8.GetBytes("MaaUma/private-service/windows-user/v1");
    private static readonly HashSet<string> PlaceholderTokens=new(StringComparer.OrdinalIgnoreCase)
    {
        "default string",
        "to be filled by o.e.m.",
        "none",
        "unknown",
        "n/a",
        "00000000",
        "ffffffff",
        "system serial number",
    };
    [StructLayout(LayoutKind.Sequential)] private struct Blob {public int Length;public IntPtr Data;}
    [DllImport("crypt32.dll",SetLastError=true,CharSet=CharSet.Unicode)]
    [return:MarshalAs(UnmanagedType.Bool)] private static extern bool CryptProtectData(ref Blob input,string description,ref Blob entropy,IntPtr reserved,IntPtr prompt,int flags,out Blob output);
    [DllImport("crypt32.dll",SetLastError=true,CharSet=CharSet.Unicode)]
    [return:MarshalAs(UnmanagedType.Bool)] private static extern bool CryptUnprotectData(ref Blob input,IntPtr description,ref Blob entropy,IntPtr reserved,IntPtr prompt,int flags,out Blob output);
    [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr memory);

    internal static int Run()
    {
        try {Console.OutputEncoding=new UTF8Encoding(false);}catch{}
        JObject response;
        try {
            if(!OperatingSystem.IsWindows())throw new IdentityException("device_identity_unavailable","unsupported_os");
            var requestText=ReadInputText();
            JObject request;
            try {request=JObject.Parse(requestText);}
            catch(Exception ex) {throw new IdentityException("device_identity_unavailable","parse_request",errorType:ex.GetType().Name);}
            response=Dispatch(request);
            response["ok"]=true;
        } catch(IdentityException error) {
            response=new JObject {["ok"]=false,["code"]=error.Code};
            if(!string.IsNullOrEmpty(error.Stage))response["stage"]=error.Stage;
            if(!string.IsNullOrEmpty(error.ErrorType))response["error_type"]=error.ErrorType;
            if(error.Win32Error!=0)response["win32_error"]=error.Win32Error;
        } catch(Exception ex) {
            response=new JObject {
                ["ok"]=false,
                ["code"]="device_identity_unavailable",
                ["stage"]="unhandled",
                ["error_type"]=ex.GetType().Name,
                ["hresult"]=ex.HResult,
            };
        }
        Console.WriteLine(response.ToString(Formatting.None));
        return (bool?)response["ok"]==true ? 0 : 2;
    }

    private static string ReadInputText()
    {
        const int maxBytes=2*1024*1024;
        using var stdin=Console.OpenStandardInput();
        using var ms=new MemoryStream();
        var buf=new byte[8192];
        while(true) {
            var read=stdin.Read(buf,0,buf.Length);
            if(read<=0)break;
            if(ms.Length+read>maxBytes)throw new IdentityException("device_identity_unavailable","request_too_large");
            ms.Write(buf,0,read);
        }
        var text=new UTF8Encoding(false,false).GetString(ms.ToArray());
        return text.Trim().TrimStart('\uFEFF').Trim();
    }

    private static JObject Dispatch(JObject request)
    {
        var action=(string?)request["action"];
        if(action is not ("identity" or "sign" or "seal" or "unseal"))throw new IdentityException("device_identity_unavailable","invalid_action");
        var deviceId=MachineDigest();
        if(action=="unseal") {
            try {
                var decrypted=Dpapi(Convert.FromBase64String((string?)request["encrypted"] ?? ""),false);
                var opened=JObject.Parse(Encoding.UTF8.GetString(decrypted));
                if((string?)opened["device_id"]!=deviceId)throw new IdentityException("device_changed","unseal_device_changed");
                return new JObject {["payload_base64"]=(string?)opened["payload_base64"]};
            } catch(IdentityException) {throw;}
              catch(DpapiException ex) {throw new IdentityException("device_credentials_unreadable","unseal_dpapi",errorType:"CryptographicException",win32Error:ex.Win32Error);}
              catch(Exception ex) {throw new IdentityException("device_credentials_unreadable","unseal_parse",errorType:ex.GetType().Name);}
        }
        using var key=LoadKey(deviceId);
        if(action=="identity")return new JObject {["device_id"]=deviceId,["public_key"]=Convert.ToBase64String(key.ExportSubjectPublicKeyInfo())};
        if(action=="sign") {
            byte[] message;
            try {message=Convert.FromBase64String((string?)request["message_base64"] ?? "");}
            catch(Exception ex) {throw new IdentityException("device_identity_unavailable","sign_decode",errorType:ex.GetType().Name);}
            if(message.Length>4096 || !Encoding.UTF8.GetString(message).StartsWith(Domain+"\n",StringComparison.Ordinal))
                throw new IdentityException("device_identity_unavailable","sign_domain");
            return new JObject {["signature"]=Convert.ToBase64String(key.SignData(message,HashAlgorithmName.SHA256,DSASignatureFormat.IeeeP1363FixedFieldConcatenation))};
        }
        var payload=(string?)request["payload_base64"] ?? "";
        try {
            if(Convert.FromBase64String(payload).Length>512*1024)throw new IdentityException("device_credentials_unreadable","seal_too_large");
        } catch(IdentityException) {throw;}
          catch(Exception ex) {throw new IdentityException("device_credentials_unreadable","seal_decode",errorType:ex.GetType().Name);}
        var plaintext=Encoding.UTF8.GetBytes(new JObject {["device_id"]=deviceId,["payload_base64"]=payload}.ToString(Formatting.None));
        try {return new JObject {["encrypted"]=Convert.ToBase64String(Dpapi(plaintext,true))};}
        catch(DpapiException ex) {throw new IdentityException("device_identity_unavailable","seal_dpapi",errorType:"CryptographicException",win32Error:ex.Win32Error);}
        finally {CryptographicOperations.ZeroMemory(plaintext);}
    }

    private static string MachineDigest() => ComputeDigestFromSources(ReadRegistrySource);

    internal static string ComputeDigestFromSources(Func<string,string?> reader)
    {
        var guid64=reader("machine_guid_64");
        if(Guid.TryParse(guid64,out var parsed64))
            return Sha256Hex(Domain+"\n"+parsed64.ToString("D"));

        var guid32=reader("machine_guid_32");
        if(Guid.TryParse(guid32,out var parsed32))
            return Sha256Hex(Domain+"\n"+parsed32.ToString("D"));

        var rawGuid=CleanToken(guid64) ?? CleanToken(guid32);
        if(rawGuid is {Length:>=6})
            return Sha256Hex(Domain+"\nmg-raw:"+rawGuid.ToLowerInvariant());

        if(Guid.TryParse(reader("hw_profile_guid"),out var hwProfile))
            return Sha256Hex(Domain+"\nhwprofile:"+hwProfile.ToString("D"));

        if(Guid.TryParse(reader("sqm_machine_id"),out var sqmId))
            return Sha256Hex(Domain+"\nsqm:"+sqmId.ToString("D"));

        if(Guid.TryParse(reader("hw_config_guid"),out var hwConfig))
            return Sha256Hex(Domain+"\nhwconfig:"+hwConfig.ToString("D"));

        var compositeKeys=new[]{"sys_manufacturer","sys_product","bios_release","board_manufacturer","board_product","win_product_id","win_install_date"};
        var parts=new List<string>();
        foreach(var k in compositeKeys) {
            var token=CleanToken(reader(k));
            if(token is not null)parts.Add(k+"="+token);
        }
        if(parts.Count>=2)
            return Sha256Hex(Domain+"\nhwreg:"+string.Join("|",parts));

        throw new IdentityException("device_identity_unavailable","machine_digest");
    }

    private static string Sha256Hex(string value) =>
        Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(value)));

    private static string? CleanToken(string? raw)
    {
        if(string.IsNullOrWhiteSpace(raw))return null;
        var trimmed=raw.Trim();
        if(trimmed.Length==0 || PlaceholderTokens.Contains(trimmed))return null;
        if(trimmed.Replace("0","").Replace("-","").Replace("{","").Replace("}","").Length==0)return null;
        return trimmed;
    }

    private static string? ReadRegistryValue(RegistryView view,string subKey,string valueName)
    {
        try {
            using var machine=RegistryKey.OpenBaseKey(RegistryHive.LocalMachine,view);
            using var sub=machine.OpenSubKey(subKey);
            var raw=sub?.GetValue(valueName);
            return raw?.ToString();
        } catch {
            return null;
        }
    }

    private static string? ReadRegistrySource(string sourceKey) => sourceKey switch {
        "machine_guid_64" => ReadRegistryValue(RegistryView.Registry64,@"SOFTWARE\Microsoft\Cryptography","MachineGuid"),
        "machine_guid_32" => ReadRegistryValue(RegistryView.Registry32,@"SOFTWARE\Microsoft\Cryptography","MachineGuid"),
        "hw_profile_guid" => ReadRegistryValue(RegistryView.Registry64,@"SYSTEM\CurrentControlSet\Control\IDConfigDB\Hardware Profiles\0001","HwProfileGuid"),
        "sqm_machine_id" => ReadRegistryValue(RegistryView.Registry64,@"SOFTWARE\Microsoft\SQMClient","MachineId"),
        "hw_config_guid" => ReadRegistryValue(RegistryView.Registry64,@"SYSTEM\HardwareConfig","LastConfig"),
        "sys_manufacturer" => ReadRegistryValue(RegistryView.Registry64,@"SYSTEM\CurrentControlSet\Control\SystemInformation","SystemManufacturer"),
        "sys_product" => ReadRegistryValue(RegistryView.Registry64,@"SYSTEM\CurrentControlSet\Control\SystemInformation","SystemProductName"),
        "bios_release" => ReadRegistryValue(RegistryView.Registry64,@"SYSTEM\CurrentControlSet\Control\SystemInformation","BIOSReleaseDate"),
        "board_manufacturer" => ReadRegistryValue(RegistryView.Registry64,@"HARDWARE\DESCRIPTION\System\BIOS","BaseBoardManufacturer"),
        "board_product" => ReadRegistryValue(RegistryView.Registry64,@"HARDWARE\DESCRIPTION\System\BIOS","BaseBoardProduct"),
        "win_product_id" => ReadRegistryValue(RegistryView.Registry64,@"SOFTWARE\Microsoft\Windows NT\CurrentVersion","ProductId"),
        "win_install_date" => ReadRegistryValue(RegistryView.Registry64,@"SOFTWARE\Microsoft\Windows NT\CurrentVersion","InstallDate"),
        _ => null,
    };

    private static IDisposable AcquireKeyLock(string folder)
    {
        var folderHash=Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(folder.ToLowerInvariant())))[..16];
        var mutexName=@"Local\MaaUma-device-identity-"+folderHash;
        try {
            var gate=new Mutex(false,mutexName);
            var held=false;
            try {
                try {held=gate.WaitOne(TimeSpan.FromSeconds(4));}
                catch(AbandonedMutexException) {held=true;}
                if(!held) {
                    gate.Dispose();
                    throw new IdentityException("device_identity_unavailable","key_lock_timeout");
                }
                return new MutexLease(gate);
            } catch(IdentityException) {throw;}
              catch(Exception) {
                if(held)try {gate.ReleaseMutex();}catch{}
                gate.Dispose();
                throw;
            }
        } catch(IdentityException) {throw;}
          catch(UnauthorizedAccessException) {}
          catch(WaitHandleCannotBeOpenedException) {}

        var lockPath=Path.Combine(folder,"device-key-v1.lock");
        var deadline=DateTime.UtcNow.AddSeconds(4);
        while(true) {
            try {
                return new FileStream(lockPath,FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.None);
            } catch(IOException) {
                if(DateTime.UtcNow>=deadline)throw new IdentityException("device_identity_unavailable","key_lock_timeout");
                Thread.Sleep(50);
            } catch(Exception ex) {
                throw new IdentityException("device_identity_unavailable","key_lock_timeout",errorType:ex.GetType().Name,win32Error:ex.HResult);
            }
        }
    }

    private sealed class MutexLease(Mutex gate):IDisposable
    {
        public void Dispose()
        {
            try {gate.ReleaseMutex();}catch{}
            gate.Dispose();
        }
    }

    private static ECDsa LoadKey(string deviceId)
    {
        var localAppData=Environment.GetEnvironmentVariable("LOCALAPPDATA");
        if(string.IsNullOrWhiteSpace(localAppData))
            localAppData=Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if(string.IsNullOrWhiteSpace(localAppData))
            throw new IdentityException("device_identity_unavailable","key_dir_missing");
        var folder=Path.Combine(localAppData,"MaaUma","PrivateService");
        try {Directory.CreateDirectory(folder);}
        catch(Exception ex) {throw new IdentityException("device_identity_unavailable","key_dir_create",errorType:ex.GetType().Name,win32Error:ex.HResult);}
        if((File.GetAttributes(folder)&FileAttributes.ReparsePoint)!=0)throw new IdentityException("device_key_unreadable","key_dir_reparse");
        var path=Path.Combine(folder,"device-key-v1.bin");
        // Serialize simultaneous activation/update processes without exposing key material.
        using var gate=AcquireKeyLock(folder);
        var needsCreate=!File.Exists(path);
        if(!needsCreate) {
            if((File.GetAttributes(path)&FileAttributes.ReparsePoint)!=0)throw new IdentityException("device_key_unreadable","key_file_reparse");
            if(new FileInfo(path).Length==0)needsCreate=true;
        }
        if(needsCreate) {
            using var created=ECDsa.Create(ECCurve.NamedCurves.nistP256);
            var privateBytes=created.ExportPkcs8PrivateKey();
            byte[]? raw=null;
            var tmpPath=Path.Combine(folder,"device-key-v1."+Guid.NewGuid().ToString("N")+".tmp");
            try {
                raw=Encoding.UTF8.GetBytes(new JObject {["device_id"]=deviceId,["private_key"]=Convert.ToBase64String(privateBytes)}.ToString(Formatting.None));
                byte[] protectedBytes;
                try {protectedBytes=Dpapi(raw,true);}
                catch(DpapiException ex) {throw new IdentityException("device_identity_unavailable","key_create_dpapi",errorType:"CryptographicException",win32Error:ex.Win32Error);}
                try {
                    using(var stream=new FileStream(tmpPath,FileMode.CreateNew,FileAccess.Write,FileShare.None)) {
                        stream.Write(protectedBytes);stream.Flush(true);
                    }
                    File.Move(tmpPath,path,overwrite:true);
                } catch(IdentityException) {throw;}
                  catch(Exception ex) {
                    try {if(File.Exists(tmpPath))File.Delete(tmpPath);}catch{}
                    throw new IdentityException("device_identity_unavailable","key_create_write",errorType:ex.GetType().Name,win32Error:ex.HResult);
                }
            } finally {CryptographicOperations.ZeroMemory(privateBytes);if(raw!=null)CryptographicOperations.ZeroMemory(raw);}
        }
        if((File.GetAttributes(path)&FileAttributes.ReparsePoint)!=0)throw new IdentityException("device_key_unreadable","key_file_reparse");
        byte[]? decrypted=null;byte[]? privateKey=null;
        try {
            try {decrypted=Dpapi(File.ReadAllBytes(path),false);}
            catch(DpapiException ex) {throw new IdentityException("device_key_unreadable","key_read_dpapi",errorType:"CryptographicException",win32Error:ex.Win32Error);}
            var record=JObject.Parse(Encoding.UTF8.GetString(decrypted));
            if((string?)record["device_id"]!=deviceId)throw new IdentityException("device_changed","key_device_changed");
            privateKey=Convert.FromBase64String((string?)record["private_key"] ?? "");
            var key=ECDsa.Create();
            try {key.ImportPkcs8PrivateKey(privateKey,out var consumed);if(consumed!=privateKey.Length || key.KeySize!=256)throw new CryptographicException();return key;}
            catch {key.Dispose();throw;}
        } catch(IdentityException) {throw;}
          catch(Exception ex) {throw new IdentityException("device_key_unreadable","key_read_parse",errorType:ex.GetType().Name);}
          finally {if(decrypted!=null)CryptographicOperations.ZeroMemory(decrypted);if(privateKey!=null)CryptographicOperations.ZeroMemory(privateKey);}
    }

    private static byte[] Dpapi(byte[] bytes,bool protect)
    {
        if(bytes.Length==0)throw new DpapiException(0);
        var input=new Blob {Length=bytes.Length,Data=Marshal.AllocHGlobal(bytes.Length)};
        var entropy=new Blob {Length=Entropy.Length,Data=Marshal.AllocHGlobal(Entropy.Length)};
        Blob output=default;
        try {
            Marshal.Copy(bytes,0,input.Data,bytes.Length);Marshal.Copy(Entropy,0,entropy.Data,Entropy.Length);
            var ok=protect ? CryptProtectData(ref input,"MaaUma private service",ref entropy,IntPtr.Zero,IntPtr.Zero,1,out output)
                           : CryptUnprotectData(ref input,IntPtr.Zero,ref entropy,IntPtr.Zero,IntPtr.Zero,1,out output);
            if(!ok)throw new DpapiException(Marshal.GetLastWin32Error());
            var result=new byte[output.Length];Marshal.Copy(output.Data,result,0,result.Length);return result;
        } finally {
            for(var i=0;i<input.Length;i++)Marshal.WriteByte(input.Data,i,0);
            Marshal.FreeHGlobal(input.Data);Marshal.FreeHGlobal(entropy.Data);
            if(output.Data!=IntPtr.Zero){for(var i=0;i<output.Length;i++)Marshal.WriteByte(output.Data,i,0);LocalFree(output.Data);}
        }
    }

    private sealed class DpapiException(int win32Error):Exception
    {
        public int Win32Error {get;}=win32Error;
    }

    private sealed class IdentityException(string code,string stage="",string errorType="",int win32Error=0):Exception(code)
    {
        public string Code {get;}=code;
        public string Stage {get;}=stage;
        public string ErrorType {get;}=errorType;
        public int Win32Error {get;}=win32Error;
    }
}
