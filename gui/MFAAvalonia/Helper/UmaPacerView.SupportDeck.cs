using System;
using System.Collections.Generic;
using Avalonia.Controls;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed partial class UmaPacerView
{
    private UmaSupportDeckInputEditor? careerSupportProfile,cutSupportProfile;
    private JToken? careerSupportProfileValue,cutSupportProfileValue;
    private bool careerSupportProfilePresent,cutSupportProfilePresent;

    private UmaSupportDeckInputEditor CreateSupportProfile(bool cut,IEnumerable<UmaSupportChoice> choices)
    {
        var editor=new UmaSupportDeckInputEditor(null,value=>{
            if(cut){cutSupportProfilePresent=true;cutSupportProfileValue=value?.DeepClone();}
            else{careerSupportProfilePresent=true;careerSupportProfileValue=value?.DeepClone();}
        },UmaSupportDeckInputEditor.FromChoices(choices),cut?"PacerCutSupportProfile":"PacerCareerSupportProfile",()=>applying);
        editor.PendingChanged+=()=>{if(applying)return;if(standaloneCutProfiles)savePreset.Content="保存配置 · 有修改";};
        return editor;
    }
    private void LoadSupportProfile(JObject settings)
    {
        var present=settings.Property("support_profile")!=null;var value=settings["support_profile"]?.DeepClone();
        if(IsCut){cutSupportProfilePresent=present;cutSupportProfileValue=value;cutSupportProfile?.Load(value);}
        else{careerSupportProfilePresent=present;careerSupportProfileValue=value;careerSupportProfile?.Load(value);}
    }
    private void CommitSupportProfileForSubmission()
    {
        var editor=IsCut?cutSupportProfile:careerSupportProfile;
        if(editor!=null&&!editor.TryCommitPending())throw new InvalidOperationException(editor.Message);
    }
    private JObject WithDeclaredSupportProfile(JObject options)
    {
        var present=IsCut?cutSupportProfilePresent:careerSupportProfilePresent;
        var value=IsCut?cutSupportProfileValue:careerSupportProfileValue;
        if(present)options["support_profile"]=value?.DeepClone()??JValue.CreateNull();
        return options;
    }
}
