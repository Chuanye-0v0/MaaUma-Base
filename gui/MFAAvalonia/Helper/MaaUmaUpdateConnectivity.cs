using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Whitelist-only display of updater network stages. Never shows raw exceptions or URLs.</summary>
public static class MaaUmaUpdateConnectivity
{
    private static readonly HashSet<string> Phases=new(){"public_catalog","authorized_catalog","patch_catalog","license_check","activation","package_download","public_cos_download","service_request"};
    private static readonly HashSet<string> Causes=new(){"timeout","dns","tls_certificate","tls_connection","connection","invalid_response","authorization_rejected","http_error","transport_error"};
    private static string? Text(JObject? value,string key)=>value?[key] is JValue token&&token.Type==JTokenType.String?(string?)token:null;
    private static string Phase(string? phase)=>phase switch {
        "public_catalog"=>"获取基础版本信息","authorized_catalog"=>"获取已授权版本信息","patch_catalog"=>"查询增量更新",
        "license_check"=>"确认授权","activation"=>"确认激活","package_download"=>"连接更新文件",
        "public_cos_download"=>"连接腾讯云文件",_=>"连接更新服务"};
    private static string Cause(string? cause)=>cause switch {
        "timeout"=>"连接超时","dns"=>"域名解析失败","tls_certificate"=>"安全证书验证失败",
        "tls_connection"=>"安全连接中断","connection"=>"连接未建立","invalid_response"=>"服务返回了异常内容",
        "authorization_rejected"=>"服务拒绝了当前授权","http_error"=>"服务返回错误",_=>"网络连接未完成"};
    public static void CopyProgress(JObject value,JObject safe)
    {
        if(Text(value,"network_phase") is {} phase&&Phases.Contains(phase))safe["network_phase"]=phase;
        if(Text(value,"network_status") is "requesting" or "received" or "failed")safe["network_status"]=value["network_status"];
        if(Text(value,"network_cause") is {} cause&&Causes.Contains(cause))safe["network_cause"]=cause;
    }
    public static string ProgressText(JObject? value)
    {
        var phase=Text(value,"network_phase");if(phase==null||!Phases.Contains(phase))return "";
        return Text(value,"network_status") switch {
            "requesting"=>"正在"+Phase(phase)+"，等待服务响应；尚未开始传输更新文件。",
            "received"=>Phase(phase)+"：已收到响应。",
            "failed"=>Phase(phase)+"失败："+Cause(Text(value,"network_cause"))+"。",
            _=>""};
    }
    public static string FailureText(JObject? result)
    {
        if(result?["network"] is JObject network&&Text(network,"schema_version")=="maauma.update_connectivity.v1"
            &&Text(network,"phase") is {} phase&&Phases.Contains(phase)&&Text(network,"cause") is {} cause&&Causes.Contains(cause))
            return Phase(phase)+"失败："+Cause(cause)+"。可点击“检查网络”定位；当前安装未修改。";
        return Text(result,"code") switch {
            "activation_required"=>"当前授权尚未确认，请先刷新状态或确认激活。",
            "product_not_allowed"=>"当前授权不包含所需内容，请联系维护者确认权限。",
            "service_unavailable"=>"更新服务未连接成功。可点击“检查网络”定位；当前安装未修改。",
            _=>""};
    }
    public static string DiagnosticText(JObject? result)
    {
        if(result?["checks"] is not JArray checks)return "网络检查未完成；当前设置、授权和安装未修改。";
        var lines=new List<string>{"网络检查结果（不验证个人权限，不修改设置）："};
        foreach(var row in checks.OfType<JObject>()) {
            var phase=Text(row,"phase");if(phase is not ("public_catalog" or "public_cos_download"))continue;
            var route=Text(row,"route");if(route is not ("system" or "direct"))continue;
            var ok=row["ok"] is JValue token&&token.Type==JTokenType.Boolean&&(bool)token;
            lines.Add((route=="system"?"系统连接":"直连")+" · "+Phase(phase)+"："+(ok?"可用":Cause(Text(row,"cause")))+"。");
        }
        lines.Add("版本查询和文件下载使用不同服务；腾讯云可访问不代表版本查询或个人授权可用。");
        return string.Join(Environment.NewLine,lines);
    }
}
