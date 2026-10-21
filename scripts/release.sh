#!/usr/bin/env bash

# Touch Dashboard GitHub Releaser

REPO_OWNER="Liburn-Bajraktari"
REPO_NAME="Touch-Dashboard"
TOKEN_FILE="$HOME/.config/touch-dashboard/.github_token"

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
shift

COMMENT="Automated desktop release triggered by release.sh"
APK_BUILD=false
APK_FILE=""
BUMP_APK=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --comment)
            COMMENT="$2"
            shift 2
            ;;
        --apk)
            APK_BUILD=true
            BUMP_APK="true"
            shift
            ;;
        *)
            if [ -f "$1" ]; then
                APK_FILE="$1"
                BUMP_APK="true"
            fi
            shift
            ;;
    esac
done

# Load or ask for token
if [ -f "$TOKEN_FILE" ]; then
    TOKEN=$(cat "$TOKEN_FILE")
    # Clean up token in case it has trailing newlines/carriage returns
    TOKEN=$(echo "$TOKEN" | tr -d '\r\n')
else
    echo -e "${CYAN}First time setup: Please enter your GitHub Personal Access Token.${NC}"
    echo -e "You can generate one at: https://github.com/settings/tokens"
    echo -e "Required scopes: ${YELLOW}repo${NC} (or fine-grained: Contents + Releases write)"
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
if '$BUMP_APK':
    data['required_apk_version'] = '$VERSION'
with open('version.json', 'w') as f:
    json.dump(data, f, indent=4)
"

echo -e "${CYAN}Committing and pushing changes...${NC}"
git add -A
git commit -m "Release: $VERSION" || true
git push origin main || true
git tag -f "$VERSION" -m "Release $VERSION" || true
git push origin "$VERSION" || true

echo -e "${CYAN}Creating Release $VERSION on GitHub...${NC}"

# Use a temporary file to avoid pipe issues
TMP_FILE=$(mktemp)
JSON_PAYLOAD=$(python3 -c "import sys, json; print(json.dumps({'tag_name': sys.argv[1], 'name': sys.argv[1], 'body': sys.argv[2]}))" "$VERSION" "$COMMENT")

HTTP_CODE=$(curl -s -w "%{http_code}" -o "$TMP_FILE" -X POST "https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/releases" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "Content-Type: application/json" \
    -d "$JSON_PAYLOAD")

BODY=$(cat "$TMP_FILE")
rm -f "$TMP_FILE"

if [ "$HTTP_CODE" == "201" ]; then
    echo -e "${GREEN}Successfully created release $VERSION!${NC}"
    RELEASE_ID=$(echo "$BODY" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('id', ''))" 2>/dev/null)
    UPLOAD_URL=$(echo "$BODY" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('upload_url', '').split('{')[0])" 2>/dev/null)
elif [ "$HTTP_CODE" == "422" ]; then
    echo -e "${YELLOW}Release $VERSION already exists on GitHub!${NC}"
    EXISTING=$(curl -s -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/releases/tags/$VERSION")
    RELEASE_ID=$(echo "$EXISTING" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('id', ''))" 2>/dev/null)
    UPLOAD_URL=$(echo "$EXISTING" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('upload_url', '').split('{')[0])" 2>/dev/null)
else
    echo -e "${RED}Failed to create release (HTTP $HTTP_CODE)${NC}"
    echo "$BODY"
    
    if [ "$HTTP_CODE" == "401" ] || [ "$HTTP_CODE" == "403" ]; then
        echo -e "${YELLOW}Invalid token. Deleting saved token file...${NC}"
        rm -f "$TOKEN_FILE"
    fi
    exit 1
fi

# Mirror to Codeberg for legacy v0.8.2 and older installations
CB_TOKEN_FILE="$HOME/.config/touch-dashboard/.codeberg_token"
if [ -f "$CB_TOKEN_FILE" ]; then
    CB_TOKEN=$(cat "$CB_TOKEN_FILE" | tr -d '\r\n')
    if [ -n "$CB_TOKEN" ]; then
        echo -e "${CYAN}Mirroring release $VERSION to Codeberg for legacy clients...${NC}"
        CB_PAYLOAD=$(python3 -c "import sys, json; print(json.dumps({'tag_name': sys.argv[1], 'target_commitish': 'main', 'name': sys.argv[1], 'body': sys.argv[2]}))" "$VERSION" "$COMMENT")
        curl -s -X POST "https://codeberg.org/api/v1/repos/liburnb/Touch-Dashboard/releases" \
            -H "Authorization: token $CB_TOKEN" \
            -H "Content-Type: application/json" \
            -d "$CB_PAYLOAD" > /dev/null 2>&1 || true
    fi
fi

if [ "$APK_BUILD" = true ]; then
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
    echo -e "${CYAN}  Bundling templates/index.html and static/ into APK and setting version $VERSION...${NC}"
    mkdir -p "$TMP_APK_DIR/assets/public" "$TMP_APK_DIR/assets/www"
    cp "templates/index.html" "$TMP_APK_DIR/assets/public/index.html"
    cp "templates/index.html" "$TMP_APK_DIR/assets/www/index.html"
    cp -r "static" "$TMP_APK_DIR/assets/public/"
    cp -r "static" "$TMP_APK_DIR/assets/www/"
    
    # Replace the placeholder APK_VERSION in the main script block
    sed -i "s/const APK_VERSION = \"dev\";/const APK_VERSION = \"$VERSION\";/g" "$TMP_APK_DIR/assets/public/index.html"
    sed -i "s/const APK_VERSION = \"dev\";/const APK_VERSION = \"$VERSION\";/g" "$TMP_APK_DIR/assets/www/index.html"
    (cd "$TMP_APK_DIR" && zip -q -r -u "$NEW_APK" assets/public assets/www)
    
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
fi

upload_asset() {
    local file="$1" content_type="$2"
    echo -e "${CYAN}Uploading $(basename "$file") to release...${NC}"

    UPLOAD_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "${UPLOAD_URL}?name=$(basename "$file")" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Accept: application/vnd.github+json" \
        -H "Content-Type: $content_type" \
        --data-binary "@$file")

    U_HTTP_CODE=$(echo "$UPLOAD_RESPONSE" | tail -n1)
    if [ "$U_HTTP_CODE" == "201" ]; then
        echo -e "${GREEN}Successfully uploaded $(basename "$file")!${NC}"
    else
        echo -e "${RED}Failed to upload $(basename "$file") (HTTP $U_HTTP_CODE)${NC}"
        echo "$UPLOAD_RESPONSE"
    fi
}

# The installers, the app's source archives (what the installers and the
# self-update download) and the APK, with SHA256SUMS so every download is
# checked before it is used (README: Install). Archives come from the tag, so
# they hold exactly the released commit.
ASSET_DIR=$(mktemp -d)
cp scripts/install.ps1 scripts/setup.sh "$ASSET_DIR/"
for format in tar.gz zip; do
    if ! git archive --format="$format" --prefix="Touch-Dashboard-${VERSION#v}/" \
            -o "$ASSET_DIR/touch-dashboard-$VERSION.$format" "refs/tags/$VERSION"; then
        echo -e "${RED}Error: could not archive tag $VERSION; nothing was uploaded.${NC}"
        exit 1
    fi
done
[ -n "$APK_FILE" ] && cp "$APK_FILE" "$ASSET_DIR/"
(cd "$ASSET_DIR" && sha256sum -- * > SHA256SUMS)

upload_asset "$ASSET_DIR/install.ps1" "text/plain"
upload_asset "$ASSET_DIR/setup.sh" "text/plain"
upload_asset "$ASSET_DIR/touch-dashboard-$VERSION.tar.gz" "application/gzip"
upload_asset "$ASSET_DIR/touch-dashboard-$VERSION.zip" "application/zip"
[ -n "$APK_FILE" ] && upload_asset "$ASSET_DIR/$(basename "$APK_FILE")" "application/vnd.android.package-archive"
upload_asset "$ASSET_DIR/SHA256SUMS" "text/plain"
rm -rf "$ASSET_DIR"

echo -e "${GREEN}Done! Your desktop clients will automatically detect this update.${NC}"
