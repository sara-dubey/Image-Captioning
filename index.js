const express = require("express");
const cors = require("cors");
const { execFile } = require("child_process");
const path = require("path");

const app = express();
app.use(cors());
app.use(express.json());

app.get("/", (req, res) => {
  res.type("text").send("Backend running. Use GET /api/scrape-all");
});

app.get("/api/scrape-all", (req, res) => {
  const py = process.env.PYTHON || "python";
  const scriptPath = path.join(__dirname, "..", "scraper", "scrape_sources.py");

  execFile(
    py,
    [scriptPath],
    {
      timeout: 20 * 60 * 1000,      // ✅ increased timeout
      maxBuffer: 50 * 1024 * 1024,  // ✅ allow up to 50MB output
    },
    (err, stdout, stderr) => {
      if (err) {
        return res.status(500).json({
          error: "Python scraper failed",
          details: err.message,
          stderr: (stderr || "").slice(0, 8000),
        });
      }

      try {
        const data = JSON.parse(stdout);
        return res.json(data);
      } catch (e) {
        return res.status(500).json({
          error: "Could not parse scraper output as JSON",
          details: e.message,
          stdout: (stdout || "").slice(0, 8000),
          stderr: (stderr || "").slice(0, 8000),
        });
      }
    }
  );
});

const PORT = process.env.PORT || 4000;
app.listen(PORT, () => console.log(`API listening on http://localhost:${PORT}`));
