using System;
using System.IO;
using Avalonia;
using Avalonia.Media.Imaging;

namespace MFAAvalonia.Helper;

/// <summary>Prefilter large portraits before small UI/native-icon sampling.</summary>
public static class UmaIconRendering
{
    public static Bitmap Scale(Bitmap source, int width)
    {
        Bitmap current = source;
        try
        {
            // A single cubic sample from 1254px to 32px misses fine outlines.
            // Half-size stages incorporate the intervening pixels first.
            while (current.PixelSize.Width / 2 >= width)
            {
                var next = current.CreateScaledBitmap(
                    new PixelSize(current.PixelSize.Width / 2, Math.Max(1, current.PixelSize.Height / 2)),
                    BitmapInterpolationMode.HighQuality);
                if (!ReferenceEquals(current, source)) current.Dispose();
                current = next;
            }
            return current.CreateScaledBitmap(new PixelSize(width,
                Math.Max(1, (int)Math.Round(source.PixelSize.Height * (double)width / source.PixelSize.Width))),
                BitmapInterpolationMode.HighQuality);
        }
        finally
        {
            if (!ReferenceEquals(current, source)) current.Dispose();
        }
    }

    public static byte[] WindowIconBytes(Bitmap source)
    {
        // Windows selects its native size instead of shrinking one huge PNG.
        int[] sizes = { 16, 20, 24, 32, 40, 48, 64, 128, 256 };
        var frames = new byte[sizes.Length][];
        for (var i = 0; i < sizes.Length; i++)
        {
            using var bitmap = Scale(source, sizes[i]);
            using var frame = new MemoryStream();
            bitmap.Save(frame);
            frames[i] = frame.ToArray();
        }
        using var output = new MemoryStream();
        using var writer = new BinaryWriter(output);
        writer.Write((ushort)0);
        writer.Write((ushort)1);
        writer.Write((ushort)sizes.Length);
        var offset = 6 + 16 * sizes.Length;
        for (var i = 0; i < sizes.Length; i++)
        {
            writer.Write((byte)(sizes[i] == 256 ? 0 : sizes[i]));
            writer.Write((byte)(sizes[i] == 256 ? 0 : sizes[i]));
            writer.Write((ushort)0);
            writer.Write((ushort)1);
            writer.Write((ushort)32);
            writer.Write(frames[i].Length);
            writer.Write(offset);
            offset += frames[i].Length;
        }
        foreach (var frame in frames) writer.Write(frame);
        return output.ToArray();
    }
}
