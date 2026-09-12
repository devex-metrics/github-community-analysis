# GitHub Discussions & Issues Analyzer

Analyze activity across GitHub repositories with interactive HTML reports. This tool fetches discussions, issues, and releases from one or more repositories and generates a self-contained HTML report with charts and tables showing:

- **Activity over time**: Discussions and issues opened/closed
- **Top contributors**: Most active users across discussions and issues
- **Contributor growth**: New contributors over time
- **Backlog trends**: Net change and cumulative open items
- **Company insights**: Contributor affiliation (when using `--fetch-profiles`)
- **Recent activity**: Full list of discussions, issues, and releases

Reports are saved to the `reports/` folder and include interactive charts powered by Chart.js.

---

Usage: python analyze_discussions.py owner/repo

Often used repos:
- github/app
- rajbos/ai-engineering-fluency
- devex-metrics/devex-metrics

Options:
- `--refresh` - Re-fetch all data from GitHub
- `--fetch-profiles` - Fetch user profiles (company, name, bio, location) for contributor analysis
  - Enables the "Company" column in the Contributors table
  - Enables the "Contributors by Company" chart
  - Note: May take several minutes for repos with many unique contributors

Example:
```
python analyze_discussions.py github/app rajbos/ai-engineering-fluency --refresh --fetch-profiles
```

Automation:
- This repository includes a GitHub Actions workflow (`.github/workflows/analyze.yml`) that:
  - Runs daily at 8:00 UTC
  - Analyzes repos specified in the `REPOS_TO_SCAN` repository variable
  - Publishes reports to GitHub Pages
- To enable: Go to Settings > Pages and set the source to "GitHub Actions"
- Repository variables to set at Settings > Secrets and variables > Actions > Variables:
  - `USERHANDLE`: Your GitHub username (enables the "My items only" filter)
  - `REPOS_TO_SCAN`: Space-separated list of repos to analyze (e.g., `github/app rajbos/ai-engineering-fluency`)

Configuration:
- Copy `.env.example` to `.env` and set your GitHub username to enable the "My items only" filter in the Updates tab:
  ```
  cp .env.example .env
  # Edit .env with your GitHub username
  ```
