# UFC Picks Backend - Deployment Guide (Render)

## Prerequisites

1. **MongoDB Atlas Account** - Free tier works fine
   - Create cluster at https://cloud.mongodb.com
   - Get connection string from Connect > Drivers

2. **Google Cloud Console** - For OAuth
   - Create project at https://console.cloud.google.com
   - Enable Google+ API
   - Create OAuth 2.0 credentials
   - Add authorized redirect URIs for production

3. **Render Account** - https://render.com (free tier available)

---

## Step 1: Prepare MongoDB Atlas

1. Go to [MongoDB Atlas](https://cloud.mongodb.com)
2. Create a new cluster (M0 free tier is fine)
3. Create database user with read/write access
4. Add `0.0.0.0/0` to IP Access List (or Render's IPs)
5. Get connection string:
   ```
   mongodb+srv://<username>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority
   ```

---

## Step 2: Configure Google OAuth

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create new project or select existing
3. Go to **APIs & Services > OAuth consent screen**
   - Configure consent screen (External)
   - Add scopes: `email`, `profile`, `openid`
4. Go to **APIs & Services > Credentials**
   - Create OAuth 2.0 Client ID (Web application)
   - Add Authorized JavaScript origins:
     ```
     https://your-app.vercel.app
     ```
   - Add Authorized redirect URIs:
     ```
     https://your-app.vercel.app
     ```
5. Copy **Client ID** and **Client Secret**

---

## Step 3: Deploy to Render

### Option A: Deploy with Blueprint (Recommended)

1. Push your code to GitHub
2. Go to [Render Dashboard](https://dashboard.render.com)
3. Click **New > Blueprint**
4. Connect your GitHub repo
5. Render will detect `render.yaml` and create the service
6. Set the required environment variables in the dashboard:
   - `MONGODB_URI` - Your MongoDB connection string
   - `GOOGLE_CLIENT_ID` - From Google Cloud Console
   - `GOOGLE_CLIENT_SECRET` - From Google Cloud Console
   - `CORS_ORIGINS` - Your Vercel frontend URL (e.g., `https://ufc-picks.vercel.app`)

### Option B: Manual Deploy

1. Go to [Render Dashboard](https://dashboard.render.com)
2. Click **New > Web Service**
3. Connect your GitHub repo
4. Configure:
   - **Name**: `ufc-picks-api`
   - **Region**: Oregon (or closest to you)
   - **Branch**: `main`
   - **Root Directory**: `backend`
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Add Environment Variables:

   | Key | Value |
   |-----|-------|
   | `MONGODB_URI` | `mongodb+srv://...` |
   | `MONGODB_DB_NAME` | `ufc_picks` |
   | `JWT_SECRET` | Generate with `openssl rand -hex 32` |
   | `JWT_ALGORITHM` | `HS256` |
   | `JWT_EXPIRE_MINUTES` | `10080` |
   | `GOOGLE_CLIENT_ID` | `xxx.apps.googleusercontent.com` |
   | `GOOGLE_CLIENT_SECRET` | `GOCSPX-xxx` |
   | `APP_ENV` | `production` |
   | `DEBUG` | `false` |
   | `CORS_ORIGINS` | `https://your-app.vercel.app` |
   | `PYTHON_VERSION` | `3.11.0` |

6. Click **Create Web Service**

---

## Step 4: Verify Deployment

1. Wait for deployment to complete (2-5 minutes)
2. Visit your Render URL: `https://ufc-picks-api.onrender.com`
3. You should see:
   ```json
   {
     "name": "UFC Picks API",
     "version": "1.0.0",
     "docs": "/docs"
   }
   ```
4. Test health endpoint: `https://ufc-picks-api.onrender.com/health`
5. View API docs: `https://ufc-picks-api.onrender.com/docs`

---

## Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `MONGODB_URI` | Yes | MongoDB Atlas connection string |
| `MONGODB_DB_NAME` | No | Database name (default: `ufc_picks`) |
| `JWT_SECRET` | Yes | Secret key for JWT tokens |
| `JWT_ALGORITHM` | No | JWT algorithm (default: `HS256`) |
| `JWT_EXPIRE_MINUTES` | No | Token expiry (default: `10080` = 7 days) |
| `GOOGLE_CLIENT_ID` | Yes | Google OAuth Client ID |
| `GOOGLE_CLIENT_SECRET` | No | Google OAuth Client Secret |
| `APP_ENV` | No | Environment (default: `development`) |
| `DEBUG` | No | Debug mode (default: `false`) |
| `CORS_ORIGINS` | Yes | Comma-separated frontend URLs |

---

## Troubleshooting

### "Connection refused" or CORS errors
- Verify `CORS_ORIGINS` includes your frontend URL (with https://)
- Check that the URL doesn't have a trailing slash

### "Database connection failed"
- Verify MongoDB Atlas IP whitelist includes `0.0.0.0/0`
- Check connection string format
- Ensure database user has correct permissions

### "Google OAuth failed"
- Verify Client ID matches between frontend and backend
- Check authorized origins in Google Console include your domains

### Logs
View logs in Render dashboard: **Service > Logs**

---

## Notes

- **Free tier**: Render free services spin down after 15 minutes of inactivity. First request after sleep takes ~30 seconds.
- **Custom domain**: Can be configured in Render dashboard under Settings > Custom Domains
- **Auto-deploy**: Enabled by default on push to main branch
