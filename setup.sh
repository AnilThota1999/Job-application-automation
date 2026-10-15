#!/bin/bash
# One-time setup for job-bot. Run:  bash setup.sh
set -e
cd "$(dirname "$0")"
echo "1/4 Creating Python environment..."
python3 -m venv venv
source venv/bin/activate
echo "2/4 Installing Python packages..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo "3/4 Installing browser for autofill..."
playwright install chromium
echo "4/4 Checking PDF library..."
if command -v brew >/dev/null 2>&1; then
  brew list pango >/dev/null 2>&1 || brew install pango
fi
export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
grep -q "DYLD_FALLBACK_LIBRARY_PATH" ~/.zshrc 2>/dev/null || \
  echo 'export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib' >> ~/.zshrc
python -c "import weasyprint; print('PDF library OK')"
mkdir -p resumes audits screenshots manual_apply
[ -f profile.json ] || { cp config/profile.example.json profile.json; echo "-> Created profile.json from template - fill in the FILL_ME values"; }
[ -f prompt.txt ] || { cp config/prompt.example.txt prompt.txt; echo "-> Created prompt.txt from template - edit it for your resume"; }
echo ""
echo "Setup complete."
[ -f base_resume.pdf ] || echo "-> Remember to copy your resume here as base_resume.pdf"
[ -n "$GEMINI_API_KEY" ] && echo "-> Gemini key found" || echo "-> Gemini key NOT found (see step 6)"
