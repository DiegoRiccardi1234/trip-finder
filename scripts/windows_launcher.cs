using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

static class Launcher {
    static async Task Pump(Stream source, Stream destination) {
        byte[] buffer = new byte[4096];
        int count;
        while ((count = await source.ReadAsync(buffer, 0, buffer.Length)) > 0) {
            await destination.WriteAsync(buffer, 0, count);
            // MCP keeps the pipes open between requests. Waiting for a full
            // FileStream buffer or EOF would deadlock initialize/tools/list.
            await destination.FlushAsync();
        }
    }

    static string Quote(string value) {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in value) {
            if (character == '\\') { slashes++; continue; }
            if (character == '"') { result.Append('\\', slashes * 2 + 1); result.Append('"'); }
            else { result.Append('\\', slashes); result.Append(character); }
            slashes = 0;
        }
        result.Append('\\', slashes * 2); result.Append('"');
        return result.ToString();
    }

    [STAThread]
    static int Main(string[] args) {
        bool stdio = Array.IndexOf(args, "--mcp-stdio") >= 0;
        try {
            string root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            string program = Path.Combine(root, "app", stdio ? "TripFinderMCP.exe" : "TripFinder.exe");
            string marker = Path.Combine(root, "app", ".tripfinder-layout.json");
            if (!File.Exists(program) || !File.Exists(marker))
                throw new IOException("Installazione incompleta: conserva TripFinder.exe insieme alla cartella app.");
            string[] forwarded = Array.ConvertAll(args, arg => arg == "--mcp-stdio" ? "--stdio" : arg);
            var start = new ProcessStartInfo(program, string.Join(" ", Array.ConvertAll(forwarded, Quote)));
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            if (!start.EnvironmentVariables.ContainsKey("TRIPFINDER_WORKSPACE"))
                start.EnvironmentVariables["TRIPFINDER_WORKSPACE"] = root;
            start.RedirectStandardInput = stdio;
            start.RedirectStandardOutput = stdio;
            start.RedirectStandardError = stdio;
            using (Process child = Process.Start(start)) {
                Task output = null, error = null;
                if (stdio) {
                    Task.Run(async () => { try { await Pump(Console.OpenStandardInput(), child.StandardInput.BaseStream); child.StandardInput.Close(); } catch (IOException) {} catch (ObjectDisposedException) {} });
                    output = Pump(child.StandardOutput.BaseStream, Console.OpenStandardOutput());
                    error = Pump(child.StandardError.BaseStream, Console.OpenStandardError());
                }
                child.WaitForExit();
                if (stdio) Task.WaitAll(output, error);
                return child.ExitCode;
            }
        } catch (Exception error) {
            if (stdio) Console.Error.WriteLine("Trip Finder: " + error.Message);
            else MessageBox.Show(error.Message, "Trip Finder", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
