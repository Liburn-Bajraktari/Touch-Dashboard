#!/usr/bin/env bash

# Touch Dashboard Codeberg Releaser

REPO_OWNER="liburnb"
REPO_NAME="Touch-Dashboard"
TOKEN_FILE="$HOME/.config/touch-dashboard/.codeberg_token"

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

if [ -z "$1" ]; then
    echo -e "${RED}Error: You must provide a version tag.${NC}"
    echo -e "Usage: ./scripts/release.sh v0.4.0"
    exit 1
fi

VERSION="$1"

# Load or ask for token
if [ -f "$TOKEN_FILE" ]; then
    TOKEN=$(cat "$TOKEN_FILE")
    # Clean up token in case it has trailing newlines/carriage returns
    TOKEN=$(echo "$TOKEN" | tr -d '\r\n')
else
    echo -e "${CYAN}First time setup: Please enter your Codeberg Personal Access Token.${NC}"
    echo -e "You can generate one at: https://codeberg.org/user/settings/applications"
    read -sp "Token: " TOKEN
    echo
    
    # Save it for next time
    mkdir -p "$(dirname "$TOKEN_FILE")"
    TOKEN=$(echo "$TOKEN" | tr -d '\r\n')
    echo "$TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
fi

echo -e "${CYAN}Bumping version in version.json...${NC}"
python3 -c "
import json
with open('version.json', 'r') as f:
    data = json.load(f)
data['desktop_version'] = '$VERSION'
if '$2':
    data['required_apk_version'] = '$VERSION'
with open('version.json', 'w') as f:
    json.dump(data, f, indent=4)
"

echo -e "${CYAN}Committing and pushing changes...${NC}"
git add version.json
git commit -m "Release: $VERSION" || true
git push origin main || true

echo -e "${CYAN}Creating Release $VERSION on Codeberg...${NC}"

# Use a temporary file to avoid pipe issues
TMP_FILE=$(mktemp)
HTTP_CODE=$(curl -s -w "%{http_code}" -o "$TMP_FILE" -X POST "https://codeberg.org/api/v1/repos/$REPO_OWNER/$REPO_NAME/releases" \
    -H "Authorization: token $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"tag_name\": \"$VERSION\", \"name\": \"$VERSION\", \"body\": \"Automated desktop release triggered by release.sh\"}")

BODY=$(cat "$TMP_FILE")
rm -f "$TMP_FILE"

if [ "$HTTP_CODE" == "201" ]; then
    echo -e "${GREEN}Successfully created release $VERSION!${NC}"
    RELEASE_ID=$(echo "$BODY" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('id', ''))" 2>/dev/null)
elif [ "$HTTP_CODE" == "409" ]; then
    echo -e "${YELLOW}Release $VERSION already exists on Codeberg!${NC}"
    EXISTING=$(curl -s -X GET "https://codeberg.org/api/v1/repos/$REPO_OWNER/$REPO_NAME/releases/tags/$VERSION" -H "Authorization: token $TOKEN")
    RELEASE_ID=$(echo "$EXISTING" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('id', ''))" 2>/dev/null)
else
    echo -e "${RED}Failed to create release (HTTP $HTTP_CODE)${NC}"
    echo "$BODY"
    
    if [ "$HTTP_CODE" == "401" ] || [ "$HTTP_CODE" == "403" ]; then
        echo -e "${YELLOW}Invalid token. Deleting saved token file...${NC}"
        rm -f "$TOKEN_FILE"
    fi
    exit 1
fi

if [ "$2" == "--apk" ]; then
    echo -e "${CYAN}Finding existing APK to rebuild...${NC}"
    OLD_APK=$(ls TouchDashboard-*.apk 2>/dev/null | head -n 1)
    if [ -z "$OLD_APK" ]; then
        echo -e "${RED}Error: No existing TouchDashboard-*.apk found in the repository to rebuild!${NC}"
        exit 1
    fi
    
    NEW_APK="TouchDashboard-${VERSION}.apk"
    echo -e "${CYAN}Rebuilding $OLD_APK into $NEW_APK with updated internal version...${NC}"
    
    # Setup temp dir for patching
    TMP_APK_DIR=$(mktemp -d)
    cp "$OLD_APK" "$TMP_APK_DIR/$NEW_APK"
    
    # Extract, patch, and re-inject index.html
    echo -e "${CYAN}  Patching internal APK version to $VERSION...${NC}"
    unzip -q "$TMP_APK_DIR/$NEW_APK" assets/public/index.html assets/www/index.html -d "$TMP_APK_DIR"
    sed -i "s/const APK_VERSION = \".*\";/const APK_VERSION = \"$VERSION\";/g" "$TMP_APK_DIR/assets/public/index.html"
    sed -i "s/const APK_VERSION = \".*\";/const APK_VERSION = \"$VERSION\";/g" "$TMP_APK_DIR/assets/www/index.html"
    (cd "$TMP_APK_DIR" && zip -q -u "$NEW_APK" assets/public/index.html assets/www/index.html)
    
    # Resign APK properly with V2 signature and zipalign
    echo -e "${CYAN}  Removing old signature and resigning with uber-apk-signer...${NC}"
    zip -q -d "$TMP_APK_DIR/$NEW_APK" "META-INF/*" || true
    
    UBER_SIGNER="$HOME/.config/touch-dashboard/uber-apk-signer.jar"
    if [ ! -f "$UBER_SIGNER" ]; then
        echo -e "${CYAN}  Downloading uber-apk-signer for proper V2 signing...${NC}"
        mkdir -p "$(dirname "$UBER_SIGNER")"
        curl -L -s -o "$UBER_SIGNER" "https://github.com/patrickfav/uber-apk-signer/releases/download/v1.3.0/uber-apk-signer-1.3.0.jar"
    fi
    
    java -jar "$UBER_SIGNER" -a "$TMP_APK_DIR/$NEW_APK" -o "$TMP_APK_DIR" > /dev/null 2>&1
    
    SIGNED_APK=$(ls "$TMP_APK_DIR"/*-aligned-debugSigned.apk 2>/dev/null | head -n 1)
    if [ -z "$SIGNED_APK" ]; then
        echo -e "${RED}Error: Failed to sign and align APK! Make sure Java is installed.${NC}"
        exit 1
    fi
    
    # Bring the new APK back to the repo root and clean up
    mv "$SIGNED_APK" "./$NEW_APK"
    rm -rf "$TMP_APK_DIR"
    if [ "$OLD_APK" != "$NEW_APK" ]; then
        rm -f "$OLD_APK"
    fi
    
    APK_FILE="$NEW_APK"
elif [ -n "$2" ] && [ -f "$2" ]; then
    APK_FILE="$2"
else
    APK_FILE=""
fi

if [ -n "$APK_FILE" ]; then
    echo -e "${CYAN}Uploading $APK_FILE to release...${NC}"
    
    UPLOAD_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "https://codeberg.org/api/v1/repos/$REPO_OWNER/$REPO_NAME/releases/$RELEASE_ID/assets" \
        -H "Authorization: token $TOKEN" \
        -F "attachment=@$APK_FILE")
    
    U_HTTP_CODE=$(echo "$UPLOAD_RESPONSE" | tail -n1)
    if [ "$U_HTTP_CODE" == "201" ]; then
        echo -e "${GREEN}Successfully uploaded $APK_FILE!${NC}"
    else
        echo -e "${RED}Failed to upload APK (HTTP $U_HTTP_CODE)${NC}"
        echo "$UPLOAD_RESPONSE"
    fi
fi

echo -e "${GREEN}Done! Your desktop clients will automatically detect this update.${NC}"
