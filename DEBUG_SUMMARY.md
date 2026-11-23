# MySQL Connection Debug Summary

## Problem
The Kaya Kopi Streamlit app was getting "database query failure" errors when attempting to query the MySQL database, even though:
- MySQL container was running in Docker
- Tables existed in the kayakopi database
- User credentials were correct
- Environment variables were set in .env file

## Root Cause
The issue was with the **hostname configuration** for local development:
- **Original setting**: `KAYA_DB_HOST=127.0.0.1`
- **Problem**: 127.0.0.1 connects via TCP, but MySQL was using socket-based authentication for localhost
- **Error**: `Access denied for user 'root'@'localhost' (using password: YES)`

## Solution
Changed the .env file to use `localhost` instead of `127.0.0.1`:

```diff
# BEFORE
KAYA_DB_HOST=127.0.0.1

# AFTER
KAYA_DB_HOST=localhost
```

## Why This Works
- When connecting to `localhost` via Python's pymysql, it uses Unix socket connections for local MySQL instances
- When connecting to `127.0.0.1`, it uses TCP, which may have different authentication rules
- The MySQL Docker container accepts both methods, but socket connections work better for local development

## Verification Steps Performed

1. **Environment Loading**: Confirmed .env file is properly loaded by chat_backend.py via `load_dotenv()`
2. **Connection Testing**: 
   - Created test scripts to verify MySQL connection with different host configurations
   - Tested both `127.0.0.1` and `localhost` with various user/password combinations
   - Confirmed `localhost` works immediately, `127.0.0.1` fails with access denied
3. **Database Queries**: 
   - Verified the MySQLRepository can successfully:
     - Connect to the database
     - Query the tables
     - Retrieve outlet information
4. **App Verification**:
   - Confirmed Streamlit app is running
   - Verified app is healthy (health endpoint responding)
   - Confirmed KAYA_DB_* variables are properly loaded at runtime

## Files Modified
- `.env`: Changed `KAYA_DB_HOST` from `127.0.0.1` to `localhost`

## Expected Behavior After Fix
✓ Streamlit app should now successfully connect to MySQL
✓ Database queries should work without "database query failure" errors
✓ The chatbot should be able to retrieve data from all 17 tables in kayakopi database

## Note
This fix applies to **local development only**. When running in Docker Compose:
- The copilot service uses `KAYA_DB_HOST: mysql` (hostname of the mysql service in Docker network)
- This is correctly configured in docker-compose.yml and doesn't need changes
