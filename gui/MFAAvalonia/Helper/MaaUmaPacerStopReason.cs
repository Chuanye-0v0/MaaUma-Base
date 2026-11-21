using System;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed record MaaUmaPacerStopPresentation(
    string Reason, string Error, string ErrorType, bool CurrentFlow, string PreviousReason, string Source,
    string Message="", string PreviousMessage="", bool PreviousIsNormalCheckpoint=false, bool ReportableFailure=false, bool CurrentSegmentDiagnostics=false);

/// <summary>Present the outer worker independently of its last completed career segment.</summary>
public static class MaaUmaPacerStopReason
{
    private static string Text(JObject? value,string key,int limit=800)
    {
        if(value?[key] is not JValue token || token.Type is JTokenType.Null or JTokenType.Undefined)return "";
        var text=token.ToString();return text.Length<=limit?text:text[..limit];
    }

    private static bool Flag(JObject? value,string key)=>value?[key] is JValue {Type:JTokenType.Boolean} token&&token.Value<bool>();
    private static bool NormalCheckpoint(string reason)=>reason is "cut_character_event_absent" or "cut_cutoff_absent";
    public static string PreviousText(MaaUmaPacerStopPresentation value)
    {
        if(value.PreviousMessage=="上一育成段结果"&&value.PreviousReason.Length>0)return "上一育成段结果："+value.PreviousReason;
        if(value.PreviousMessage.Length>0)return value.PreviousMessage.StartsWith("上一育成段",StringComparison.Ordinal)
            ? value.PreviousMessage : "上一育成段："+value.PreviousMessage;
        return value.PreviousReason switch {
            "cut_character_event_absent"=>"上一育成段：角色事件后无切，按批次规则处理重开",
            "cut_cutoff_absent"=>"上一育成段：截止点未发现才能出众，按批次规则处理重开",
            ""=>"",_=>"上一育成段结果："+value.PreviousReason };
    }

    public static MaaUmaPacerStopPresentation Resolve(
        JObject? cut,JObject? record,JObject? control,bool cutMode,bool active,
        JObject? currentStopContext=null,JObject? lastSegmentResult=null)
    {
        var oldReason=Text(record,"stop_reason",240);
        var previousReason=Text(lastSegmentResult,"reason",240);
        var previousMessage=Text(lastSegmentResult,"message");
        var normal=Flag(lastSegmentResult,"is_normal_checkpoint")||cutMode&&NormalCheckpoint(previousReason.Length>0?previousReason:oldReason);
        if(Text(currentStopContext,"schema_version",80)=="maauma.pacer_stop_context.v1") {
            normal=Flag(lastSegmentResult,"is_normal_checkpoint");
            if(previousReason.Length==0&&normal)previousReason=oldReason;
            var source=Text(currentStopContext,"source",80);
            var currentDiagnostics=source=="segment" || source=="batch_stop"&&Flag(lastSegmentResult,"is_current_attempt")&&!normal;
            return new(Text(currentStopContext,"reason",240),currentDiagnostics?Text(record,"error"):"",
                currentDiagnostics?Text(record,"error_type",160):"",true,previousReason,
                source,Text(currentStopContext,"message"),previousMessage,
                normal,Flag(currentStopContext,"reportable_failure"),currentDiagnostics);
        }

        // Old snapshots have no structured outer context. Do not infer a pause's
        // author or reuse a normally completed cut segment as its current error.
        var state=Text(control,"state",80);
        var runtime=control?["runtime"] as JObject;
        var license=runtime?["runtime_license"] as JObject;
        if(Text(license,"status",80)=="paused"&&state=="paused")
            return new(Text(license,"code",240),"","",true,oldReason,"runtime_license",
                Text(license,"message"),"",normal,true);
        var controlError=Text(control,"last_error");
        if(cutMode&&controlError.Length>0&&!NormalCheckpoint(controlError))
            return new(controlError,"","",true,oldReason==controlError?"":oldReason,"control",PreviousIsNormalCheckpoint:normal,ReportableFailure:true);
        if(cutMode&&control!=null&&normal) {
            var note=Text(runtime,"note");
            if(NormalCheckpoint(note))note="";
            if(state is "paused" or "pause_requested")
                return new(state,"","",true,oldReason,"outer_control",note.Length>0?note:
                    state=="paused"?"已暂停，当前育成已保留；暂停来源未记录，可查看详情后手动继续。":"正在等待后台确认暂停。",
                    PreviousMessage:previousMessage,PreviousIsNormalCheckpoint:true);
            if(active)return new("","","",true,oldReason,"outer_control",note.Length>0?note:"外层批次仍在运行，按当前步骤处理后续流程。",
                PreviousMessage:previousMessage,PreviousIsNormalCheckpoint:true);
        }
        var batchReason=cutMode&&!active?Text(cut,"stop_reason",240):"";
        if(batchReason.Length>0&&batchReason==oldReason&&!normal)
            return new(oldReason,Text(record,"error"),Text(record,"error_type",160),false,"","career_record",ReportableFailure:true);
        if(batchReason.Length>0&&!NormalCheckpoint(batchReason))
            return new(batchReason,"","",true,oldReason==batchReason?"":oldReason,"cut_batch",PreviousIsNormalCheckpoint:normal,ReportableFailure:true);
        if(cutMode&&control!=null&&normal&&(state is "stopped" or "idle"))
            return new(state,"","",true,oldReason,"outer_control","本轮已停止，当前育成已保留。",PreviousIsNormalCheckpoint:true);
        if(batchReason.Length>0&&batchReason==oldReason)
            return new(oldReason,Text(record,"error"),Text(record,"error_type",160),false,"","career_record",ReportableFailure:!normal);
        if(batchReason.Length>0)
            return new(batchReason,"","",true,oldReason==batchReason?"":oldReason,"cut_batch",PreviousIsNormalCheckpoint:normal,ReportableFailure:!normal);
        return new(oldReason,Text(record,"error"),Text(record,"error_type",160),false,"","career_record",ReportableFailure:oldReason.Length>0&&!normal);
    }
}
