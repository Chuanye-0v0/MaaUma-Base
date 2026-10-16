using Avalonia.Controls;
using Avalonia.Media;
namespace MFAAvalonia.Views.UserControls.Settings;
public partial class VersionUpdateSettingsUserControl : UserControl {
 public VersionUpdateSettingsUserControl() { Content=new TextBlock {Text=MFAAvalonia.Helper.MaaUmaActivationView.UpdateNotice,Margin=new Avalonia.Thickness(24),TextWrapping=TextWrapping.Wrap}; }
}
