#!/bin/bash

echo "Starting backend server on port 8000..."
source venv/Scripts/activate
cd backend
python main.py &
BACKEND_PID=$!

cd ../frontend
echo "Starting frontend server on port 3000..."
PORT=3000 npx react-scripts start &
FRONTEND_PID=$!

echo ""
echo "Backend (PID $BACKEND_PID):  http://localhost:8000"
echo "Frontend (PID $FRONTEND_PID): http://localhost:3000"
echo ""

wait
