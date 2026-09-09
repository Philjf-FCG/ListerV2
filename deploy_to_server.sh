#!/bin/bash

# Lister Deployment Script for Remote Server
# This script copies the app to a remote server and sets it up

REMOTE_HOST="root@192.168.1.37"
REMOTE_DIR="/opt/lister"

echo "=== Lister Deployment Script ==="
echo ""

# Check if .env exists
if [ ! -f "backend/.env" ]; then
    echo "Creating .env from .env.example..."
    cp backend/.env.example backend/.env
    echo "Please edit backend/.env with your credentials before running this script again."
    exit 1
fi

echo "Step 1: Creating remote directory..."
ssh $REMOTE_HOST "mkdir -p $REMOTE_DIR"

echo ""
echo "Step 2: Copying application files..."
scp -r backend/app $REMOTE_HOST:$REMOTE_DIR/
scp backend/.env.example $REMOTE_HOST:$REMOTE_DIR/.env.example
scp backend/requirements.txt $REMOTE_HOST:$REMOTE_DIR/

echo ""
echo "Step 3: Setting up Python environment on remote server..."
ssh $REMOTE_HOST "cd $REMOTE_DIR && python3 -m venv .venv"
ssh $REMOTE_HOST "cd $REMOTE_DIR && .venv/bin/pip install --upgrade pip"
ssh $REMOTE_HOST "cd $REMOTE_DIR && .venv/bin/pip install -r requirements.txt"

echo ""
echo "Step 4: Configuring environment..."
# Note: User needs to manually copy their .env file with credentials
echo "IMPORTANT: Copy your .env file (with Google OAuth and eBay credentials) to the server:"
echo " scp backend/.env $REMOTE_HOST:$REMOTE_DIR/"
echo ""
echo "Then configure it by editing $REMOTE_DIR/.env on the remote server."
echo ""

echo "=== Deployment Complete ==="
echo ""
echo "Next steps:"
echo "1. SSH into $REMOTE_HOST"
echo "2. cd $REMOTE_DIR"
echo "3. Edit .env with your credentials (Google OAuth, eBay API, etc.)"
echo "4. Run: source .venv/bin/activate && uvicorn app.main:app --host 0.0.0.0 --port 8010"
echo ""
echo "For Google OAuth setup instructions, see README.md section 'Google Photos'"
echo ""
