using Avalonia.Controls;
using MFAAvalonia.Helper;
using Microsoft.Extensions.DependencyInjection;

namespace MFAAvalonia.Views.UserControls.Settings;
public partial class VersionUpdateSettingsUserControl : UserControl
{
    public VersionUpdateSettingsUserControl() : this(App.Services.GetRequiredService<MaaUmaActivationView>()) { }
    public VersionUpdateSettingsUserControl(MaaUmaActivationView updates)
    {
        Name="MaaUmaUpdateGroup";Content=updates;
    }
}
