using System;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

internal static class MaaUmaPacerRuntimeLicense
{
    internal static bool TryDescribeOffline(JToken? value, out string message)
    {
        message = "";
        if (value is not JObject license || license["status"] is not JValue status ||
            status.Type != JTokenType.String || (string?)status != "offline_grace")
            return false;

        var remaining = license["remaining_seconds"];
        var seconds = remaining is JValue number &&
            number.Type is JTokenType.Integer or JTokenType.Float
                ? (double)number : double.NaN;
        string duration;
        if (!double.IsFinite(seconds) || seconds < 0)
            duration = "剩余时间待刷新";
        else
        {
            var span = TimeSpan.FromSeconds(Math.Ceiling(Math.Min(seconds, 86400)));
            duration = span.TotalMinutes < 1 ? $"剩余{(int)span.TotalSeconds}秒" :
                span.TotalHours < 1 ? $"剩余{(int)span.TotalMinutes}分" :
                $"剩余{(int)span.TotalHours}小时{span.Minutes:00}分";
        }
        var code = license["code"] is JValue { Type: JTokenType.String } reason
            ? reason.Value<string>() : null;
        var cause = code switch {
            "service_tls_verification_failed" => "授权连接的 TLS 证书验证失败，未接受该连接或回包",
            "service_html_intercepted" => "授权连接返回了 HTML 网页，未取得授权服务回包",
            "service_response_incomplete" => "授权服务回包传输中断，未取得完整回包",
            _ => "暂时无法连接授权服务"
        };
        message = $"{cause}，正在使用本次运行的授权宽限（{duration}）；当前育成继续运行。";
        return true;
    }
}
