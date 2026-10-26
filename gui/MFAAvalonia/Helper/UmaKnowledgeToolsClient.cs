using System;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Fixed loopback data exchange; never exposes a native callback to the web page.</summary>
public sealed class UmaKnowledgeToolsClient
{
    private readonly UmaKnowledgeToolsLauncher launcher;
    private readonly Func<JObject,Task<JObject>>? exchange;
    public UmaKnowledgeToolsClient(UmaKnowledgeToolsLauncher? launcher=null,Func<JObject,Task<JObject>>? exchange=null)
    {this.launcher=launcher??new();this.exchange=exchange;}
    public Task<string> OpenAsync(string? contextId=null)=>launcher.OpenAsync(contextId);
    public async Task<string> ImportAsync(JObject context)
    {
        var result=await RequestAsync(new JObject{["action"]="integration.context.import",["context"]=context.DeepClone()});
        var id=(string?)result["context_id"];if(string.IsNullOrWhiteSpace(id))throw new InvalidOperationException("工具箱未返回本次资料编号，当前配置保留。");
        return id;
    }
    public async Task<JObject?> DraftAsync(string contextId)
    {
        var result=await RequestAsync(new JObject{["action"]="integration.draft.get",["context_id"]=contextId});
        if((string?)result["context_id"]!=contextId)throw new InvalidOperationException("工具箱返回了其他资料的草稿，未采用。");
        if(result["draft"]==null||result["draft"]!.Type==JTokenType.Null)return null;
        if(result["draft"] is not JObject draft||(string?)draft["schema_version"]!="uma.toolbox.draft.v1")
            throw new InvalidOperationException("工具箱草稿格式暂不匹配，当前配置保留。");
        return (JObject)draft.DeepClone();
    }
    public async Task<JObject> RequestAsync(JObject command)
    {
        JObject response;
        if(exchange!=null)response=await exchange((JObject)command.DeepClone());
        else {
            await launcher.EnsureReadyAsync();
            using var handler=new HttpClientHandler{UseProxy=false,AllowAutoRedirect=false};
            using var client=new HttpClient(handler){Timeout=TimeSpan.FromSeconds(15)};
            using var body=new StringContent(command.ToString(Formatting.None),Encoding.UTF8,"application/json");
            using var result=await client.PostAsync(new Uri(launcher.BrowserUrl,"api/tool"),body);
            if(!result.IsSuccessStatusCode)throw new InvalidOperationException("本地工具箱暂未完成请求；当前草稿保留。");
            var text=await result.Content.ReadAsStringAsync();response=JObject.Parse(text);
        }
        if((bool?)response["ok"]==false)throw new InvalidOperationException((string?)response["error"]??"工具箱未完成请求，原输入保留。");
        // The hub returns the tool's result under result; tests may use the same wire shape.
        return response["result"] is JObject data?(JObject)data.DeepClone():response;
    }
}
