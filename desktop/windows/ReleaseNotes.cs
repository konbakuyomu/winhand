using System.Text.RegularExpressions;

namespace Winhand.Desktop;

internal enum NoteKind { Heading, Bullet, Paragraph }

internal sealed record NoteLine(NoteKind Kind, string Text);

/// <summary>Release notes arrive as Markdown (a heading, then one bullet per change); the update
/// card shows them as headings, bullets and paragraphs instead of the raw marks.</summary>
internal static partial class ReleaseNotes
{
    public static List<NoteLine> Parse(string markdown, string? version)
    {
        var lines = new List<NoteLine>();
        foreach (var raw in markdown.Replace("\r\n", "\n").Split('\n'))
        {
            var line = raw.Trim();
            if (line.Length == 0 || RulePattern().IsMatch(line))
                continue; // blank lines and --- / *** rules only separate blocks
            if (line.StartsWith('#'))
            {
                var heading = Inline(line.TrimStart('#').Trim());
                // the card already says which version these notes are for: "## winhand 0.6.0" adds nothing
                if (version is not null && Regex.Replace(heading, "^winhand\\s+", "", RegexOptions.IgnoreCase) == version)
                    continue;
                lines.Add(new NoteLine(NoteKind.Heading, heading));
            }
            else if (line.Length > 2 && line[0] is '-' or '*' or '+' && line[1] == ' ')
                lines.Add(new NoteLine(NoteKind.Bullet, Inline(line[2..].Trim())));
            else
                lines.Add(new NoteLine(NoteKind.Paragraph, Inline(line)));
        }
        return lines;
    }

    /// <summary>Links keep their text; ** bold and `code` marks go (not "__": __init__.py stays).</summary>
    public static string Inline(string text)
    {
        text = LinkPattern().Replace(text, "$1");
        return text.Replace("**", "").Replace("`", "");
    }

    [GeneratedRegex(@"!?\[([^\]]*)\]\([^)]*\)")]
    private static partial Regex LinkPattern();

    [GeneratedRegex(@"^([-*_]\s*){3,}$")]
    private static partial Regex RulePattern();
}
