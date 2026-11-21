using System;
using System.IO;
using System.Runtime.InteropServices;
using Avalonia;
using Avalonia.Media.Imaging;
using Avalonia.Platform;
using SkiaSharp;

namespace MFAAvalonia.Helper;

/// <summary>Prefilter large portraits and apply premultiplied area + unsharp-mask sampling for small UI icons.</summary>
public static class UmaIconRendering
{
    public static Bitmap LoadBestBitmap(Stream stream, bool isIco = false)
    {
        using var copy = new MemoryStream();
        stream.CopyTo(copy);
        var data = copy.ToArray();
        if ((isIco || LooksLikeIco(data)) && TryExtractLargestIcoPngFrame(data, out var pngOffset, out var pngLength))
        {
            using var pngStream = new MemoryStream(data, pngOffset, pngLength, writable: false);
            return new Bitmap(pngStream);
        }

        using var fallbackStream = new MemoryStream(data, writable: false);
        return new Bitmap(fallbackStream);
    }

    private static bool LooksLikeIco(byte[] data) =>
        data.Length >= 22 && data[0] == 0 && data[1] == 0 && data[2] == 1 && data[3] == 0;

    private static bool TryExtractLargestIcoPngFrame(byte[] data, out int bestOffset, out int bestLength)
    {
        bestOffset = 0;
        bestLength = 0;
        if (!LooksLikeIco(data))
            return false;

        int count = data[4] | (data[5] << 8);
        if (count <= 0 || 6 + count * 16 > data.Length)
            return false;

        long bestRank = -1;
        for (var i = 0; i < count; i++)
        {
            var entry = 6 + i * 16;
            int w = data[entry] == 0 ? 256 : data[entry];
            int h = data[entry + 1] == 0 ? 256 : data[entry + 1];
            int bpp = data[entry + 6] | (data[entry + 7] << 8);
            int size = BitConverter.ToInt32(data, entry + 8);
            int offset = BitConverter.ToInt32(data, entry + 12);
            if (size <= 8 || offset < 0 || offset + size > data.Length)
                continue;

            // Embedded PNG frame signature: 89 50 4E 47 0D 0A 1A 0A
            bool isPng = data[offset] == 0x89 && data[offset + 1] == 0x50
                         && data[offset + 2] == 0x4E && data[offset + 3] == 0x47;
            if (!isPng)
                continue;

            long rank = ((long)(w * h) << 24) | ((long)bpp << 16) | (uint)Math.Min(size, 0xFFFF);
            if (rank > bestRank)
            {
                bestRank = rank;
                bestOffset = offset;
                bestLength = size;
            }
        }

        return bestRank >= 0;
    }

    public static Bitmap Scale(Bitmap source, int width)
    {
        width = Math.Max(1, width);
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

    /// <summary>
    /// Downscales <paramref name="source"/> to exact physical <paramref name="width"/> pixels using progressive halving,
    /// premultiplied 2x2 area integration, and a halo-free premultiplied 3x3 Gaussian unsharp mask so fine anime
    /// eyelashes, ear stripes, and line-art remain crisp at 28-64px.
    /// </summary>
    public static Bitmap ScaleCrisp(Bitmap source, int width, double sharpenAmount = 0.56)
    {
        width = Math.Max(1, width);
        if (source.PixelSize.Width <= 2 || source.PixelSize.Height <= 2 || source.PixelSize.Width <= width)
            return Scale(source, width);

        var height = Math.Max(1, (int)Math.Round(source.PixelSize.Height * (double)width / source.PixelSize.Width));
        var w2 = width * 2;
        var h2 = height * 2;

        try
        {
            using var pre = Scale(source, w2);
            using var encoded = new MemoryStream();
            pre.Save(encoded);
            encoded.Position = 0;

            using var decoded = SKBitmap.Decode(encoded);
            if (decoded is null)
                return Scale(source, width);

            using var premul = new SKBitmap(new SKImageInfo(w2, h2, SKColorType.Bgra8888, SKAlphaType.Premul));
            if (!decoded.ScalePixels(premul, new SKSamplingOptions(SKCubicResampler.Mitchell)))
                return Scale(source, width);

            var srcSpan = premul.GetPixelSpan();
            if (srcSpan.Length < w2 * h2 * 4)
                return Scale(source, width);

            var box = new float[width * height * 4];
            var srcStride = w2 * 4;
            for (var y = 0; y < height; y++)
            {
                var r0 = (y * 2) * srcStride;
                var r1 = r0 + srcStride;
                var dstRow = y * width * 4;
                for (var x = 0; x < width; x++)
                {
                    var c0 = x * 8;
                    var c1 = c0 + 4;
                    var d = dstRow + x * 4;
                    for (var ch = 0; ch < 4; ch++)
                    {
                        box[d + ch] = 0.25f * (
                            srcSpan[r0 + c0 + ch]
                            + srcSpan[r0 + c1 + ch]
                            + srcSpan[r1 + c0 + ch]
                            + srcSpan[r1 + c1 + ch]);
                    }
                }
            }

            var outBytes = new byte[width * height * 4];
            var amount = (float)Math.Clamp(sharpenAmount, 0.0, 1.2);
            int[] kernel = { 1, 2, 1 };

            for (var y = 0; y < height; y++)
            {
                for (var x = 0; x < width; x++)
                {
                    var idx = (y * width + x) * 4;
                    var origA = Math.Clamp(box[idx + 3], 0f, 255f);
                    if (amount > 0.001f && origA > 1.0f)
                    {
                        float blurB = 0f, blurG = 0f, blurR = 0f;
                        for (var ky = -1; ky <= 1; ky++)
                        {
                            var yy = Math.Clamp(y + ky, 0, height - 1);
                            var wy = kernel[ky + 1];
                            for (var kx = -1; kx <= 1; kx++)
                            {
                                var xx = Math.Clamp(x + kx, 0, width - 1);
                                var w = wy * kernel[kx + 1];
                                var nIdx = (yy * width + xx) * 4;
                                blurB += w * box[nIdx];
                                blurG += w * box[nIdx + 1];
                                blurR += w * box[nIdx + 2];
                            }
                        }

                        blurB *= 1f / 16f;
                        blurG *= 1f / 16f;
                        blurR *= 1f / 16f;

                        var b = Math.Clamp(box[idx] + amount * (box[idx] - blurB), 0f, origA);
                        var g = Math.Clamp(box[idx + 1] + amount * (box[idx + 1] - blurG), 0f, origA);
                        var r = Math.Clamp(box[idx + 2] + amount * (box[idx + 2] - blurR), 0f, origA);

                        outBytes[idx] = (byte)Math.Clamp(MathF.Round(b), 0f, 255f);
                        outBytes[idx + 1] = (byte)Math.Clamp(MathF.Round(g), 0f, 255f);
                        outBytes[idx + 2] = (byte)Math.Clamp(MathF.Round(r), 0f, 255f);
                        outBytes[idx + 3] = (byte)Math.Clamp(MathF.Round(origA), 0f, 255f);
                    }
                    else
                    {
                        outBytes[idx] = (byte)Math.Clamp(MathF.Round(box[idx]), 0f, 255f);
                        outBytes[idx + 1] = (byte)Math.Clamp(MathF.Round(box[idx + 1]), 0f, 255f);
                        outBytes[idx + 2] = (byte)Math.Clamp(MathF.Round(box[idx + 2]), 0f, 255f);
                        outBytes[idx + 3] = (byte)Math.Clamp(MathF.Round(origA), 0f, 255f);
                    }
                }
            }

            var wb = new WriteableBitmap(
                new PixelSize(width, height),
                new Vector(96, 96),
                PixelFormat.Bgra8888,
                AlphaFormat.Premul);
            using (var fb = wb.Lock())
            {
                var rowBytes = width * 4;
                for (var y = 0; y < height; y++)
                {
                    Marshal.Copy(outBytes, y * rowBytes, fb.Address + y * fb.RowBytes, rowBytes);
                }
            }

            return wb;
        }
        catch
        {
            return Scale(source, width);
        }
    }

    public static byte[] WindowIconBytes(Bitmap source)
    {
        // Windows selects its native size instead of shrinking one huge PNG.
        int[] sizes = { 16, 20, 24, 32, 40, 48, 64, 128, 256 };
        var frames = new byte[sizes.Length][];
        for (var i = 0; i < sizes.Length; i++)
        {
            var sharpen = sizes[i] <= 48 ? 0.55 : (sizes[i] <= 64 ? 0.30 : 0.0);
            using var bitmap = sharpen > 0.0 ? ScaleCrisp(source, sizes[i], sharpen) : Scale(source, sizes[i]);
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
