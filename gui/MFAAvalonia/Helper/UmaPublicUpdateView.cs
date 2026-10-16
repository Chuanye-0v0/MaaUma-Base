using Avalonia.Controls;
using Avalonia.Media;
namespace MFAAvalonia.Helper;
public sealed class UmaPublicUpdateView : UserControl {
 public UmaPublicUpdateView() { Content=new TextBlock {Text=MaaUmaActivationView.UpdateNotice,Margin=new Avalonia.Thickness(24),TextWrapping=TextWrapping.Wrap}; }
}
