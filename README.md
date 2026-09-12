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

Configuration:
- Copy `.env.example` to `.env` and set your GitHub username to enable the "My items only" filter in the Updates tab:
  ```
  cp .env.example .env
  # Edit .env with your GitHub username
  ```
