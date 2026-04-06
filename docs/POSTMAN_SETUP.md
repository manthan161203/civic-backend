# Postman Collection Setup Guide

## Overview

This guide provides step-by-step instructions for importing and using the Civic API Postman Collection for testing and development.

## Files

- **Civic_API_Postman_Collection.json** - Complete API collection with all endpoints

## Quick Start

### 1. Import Collection

1. Open Postman
2. Click **Import** button (top-left)
3. Select **File** tab
4. Browse and select `Civic_API_Postman_Collection.json`
5. Click **Import**

### 2. Set Environment Variables

After importing, configure the following variables in Postman:

**In the collection settings, set these variables:**

```
baseUrl = http://localhost:8000
accessToken = (will be set after login)
refreshToken = (will be set after login)
userId = (will be set after fetching user)
issueId = (will be set after creating/fetching issue)
taskId = (will be set after fetching task)
adminId = (will be set after creating admin)
districtId = (will be set from locations list)
talukaId = (will be set from talukas list)
```

## API Endpoints Included

### Authentication (4 endpoints)
- `POST /auth/login` - Send OTP
- `POST /auth/verify-otp` - Verify OTP & get tokens
- `POST /auth/refresh-token` - Refresh access token
- `GET /auth/me` - Get current user

### Users (2 endpoints)
- `GET /users/{userId}` - Get user profile
- `PUT /users/{userId}` - Update user profile

### Issues (5 endpoints)
- `POST /issues` - Create issue
- `GET /issues` - List issues (with filters)
- `GET /issues/{issueId}` - Get issue details
- `PATCH /issues/{issueId}/status` - Update issue status
- `PATCH /issues/{issueId}/resolve` - Resolve issue

### Tasks (3 endpoints)
- `GET /tasks` - Get worker tasks
- `GET /tasks/{taskId}` - Get task details
- `PATCH /tasks/{taskId}/status` - Update task status

### Admin (5 endpoints)
- `GET /admin/reports` - Get admin reports
- `POST /admin/sub-admins` - Create sub-admin
- `GET /admin/sub-admins` - List sub-admins
- `PUT /admin/sub-admins/{adminId}` - Update sub-admin
- `DELETE /admin/sub-admins/{adminId}` - Delete sub-admin

### Locations (3 endpoints)
- `GET /locations/districts` - Get districts
- `GET /locations/districts/{districtId}/talukas` - Get talukas
- `GET /locations/talukas/{talukaId}/wards` - Get wards

**Total: 22 endpoints**

## Testing Workflow

### Step 1: Authentication Flow

1. **Send OTP**
   - Go to `Authentication > Send Login OTP`
   - Update `phone` with your test number
   - Send request
   - You'll get an OTP sent to the number

2. **Verify OTP**
   - Go to `Authentication > Verify OTP & Login`
   - Enter the OTP you received
   - Send request
   - Copy the `access_token` from response and paste into `accessToken` variable
   - Copy the `refresh_token` and paste into `refreshToken` variable

3. **Get Current User**
   - Go to `Authentication > Get Current User`
   - Send request
   - Copy the `id` from response and save as `userId` variable

### Step 2: Test User Endpoints

1. **Get User Profile**
   - Go to `Users > Get User Profile`
   - Tests that user data can be fetched

2. **Update User Profile**
   - Go to `Users > Update User Profile`
   - Update JSON body with new data
   - Tests that user can update their profile

### Step 3: Test Issues

1. **Create Issue**
   - Go to `Issues > Create Issue`
   - Update location coordinates and description
   - Send request
   - Copy the returned `id` and save as `issueId` variable

2. **Get Issues List**
   - Go to `Issues > Get Issues List`
   - Optional: enable filters (status, category, search)
   - Send request to see paginated results

3. **Get Issue Details**
   - Go to `Issues > Get Issue Details`
   - Send request to view details of the issue created above

4. **Update Issue Status**
   - Go to `Issues > Update Issue Status`
   - Change status to `in_progress`
   - Send request

5. **Resolve Issue**
   - Go to `Issues > Resolve Issue`
   - Add resolution notes
   - Send request to mark issue as resolved

### Step 4: Test Tasks

1. **Get Worker Tasks**
   - Go to `Tasks > Get Worker Tasks`
   - Send request to see assigned tasks

2. **Get Task Details**
   - Go to `Tasks > Get Task Details`
   - Send request for details

3. **Update Task Status**
   - Go to `Tasks > Update Task Status`
   - Change status to `in_progress`
   - Send request

### Step 5: Test Admin Functions

1. **Get Reports**
   - Go to `Admin > Get Reports Summary`
   - Send request to view admin dashboard data

2. **Create Sub-Admin**
   - Go to `Admin > Create Sub-Admin`
   - Update name, phone, district_id
   - Send request
   - Copy the returned `id` and save as `adminId` variable

3. **List Sub-Admins**
   - Go to `Admin > List Sub-Admins`
   - Send request to see all sub-admins

4. **Update Sub-Admin**
   - Go to `Admin > Update Sub-Admin`
   - Update the admin details
   - Send request

5. **Delete Sub-Admin**
   - Go to `Admin > Delete Sub-Admin`
   - Send request to delete the admin

### Step 6: Test Locations

1. **Get Districts**
   - Go to `Locations > Get Districts`
   - Send request
   - Copy a `district_id` and save as `districtId` variable

2. **Get Talukas**
   - Go to `Locations > Get Talukas`
   - Send request to see talukas for the district
   - Copy a `taluka_id` and save as `talukaId` variable

3. **Get Wards**
   - Go to `Locations > Get Wards`
   - Send request to see wards for the taluka

## Advanced Features

### Using Test Scripts

Each request can have test scripts that:
- Extract tokens from responses
- Validate response status codes
- Extract IDs for use in subsequent requests

**Example test script to add to Verify OTP request:**

```javascript
if (pm.response.code === 200) {
    var jsonData = pm.response.json();
    pm.environment.set("accessToken", jsonData.access_token);
    pm.environment.set("refreshToken", jsonData.refresh_token);
}
```

### Error Handling

Common error responses:

| Status | Error | Solution |
|--------|-------|----------|
| 401 | Unauthorized | AccessToken expired, use refresh-token endpoint |
| 403 | Forbidden | User doesn't have permission for this action |
| 404 | Not Found | Resource doesn't exist or ID is incorrect |
| 422 | Validation Error | Check request body matches schema |
| 500 | Server Error | Check backend logs |

### Rate Limiting

The API has rate limiting:
- 100 requests per minute per user
- If limit exceeded, wait 60 seconds before retrying

## Environment Setup

### Local Development
```
baseUrl = http://localhost:8000
```

### Staging
```
baseUrl = https://staging-api.civic.local
accessToken = (obtained from auth flow)
```

### Production
```
baseUrl = https://api.civic.local
accessToken = (obtained from auth flow)
```

## Best Practices

1. **Save Sensitive Data Securely**
   - Use Postman environment variables for tokens
   - Never commit tokens to version control
   - Use `.gitignore` for postman-env.json

2. **Test in Order**
   - Start with authentication
   - Then test user operations
   - Finally test complex workflows

3. **Use Descriptive Names**
   - Name your requests clearly
   - Add comments in request descriptions
   - Document custom headers

4. **Backup Collection**
   - Export collection regularly
   - Version your endpoints
   - Track changes in git

## Troubleshooting

### Issue: "Cannot connect to localhost:8000"
- Ensure backend is running: `python run.py`
- Check if port 8000 is available
- Verify firewall isn't blocking the port

### Issue: "Unauthorized - Invalid token"
- Token may have expired
- Re-run the Verify OTP request
- Update accessToken variable with new token

### Issue: "Validation Error - Missing required field"
- Check request body JSON syntax
- Ensure all required fields are present
- Compare with example in collection

### Issue: "Cannot find variable {{variableName}}"
- Ensure variable is set in Postman environment
- Check spelling matches exactly
- Verify variable is enclosed in {{}}

## Integration with CI/CD

### Running Collection Tests with Newman

```bash
# Install Newman (CLI tool for Postman)
npm install -g newman

# Run collection tests
newman run Civic_API_Postman_Collection.json \
  --environment postman-env.json \
  --reporters json,html

# Run specific folder
newman run Civic_API_Postman_Collection.json \
  --folder "Issues"
```

### GitHub Actions Example

```yaml
- name: Test API with Postman Collection
  run: |
    npm install -g newman
    newman run Civic_API_Postman_Collection.json \
      --environment postman-env.json \
      --timeout-request 5000
```

## Support & Documentation

For detailed API documentation, see:
- [API_DOCUMENTATION.md](API_DOCUMENTATION.md) - Complete endpoint reference
- [INTEGRATION_EXAMPLES.md](INTEGRATION_EXAMPLES.md) - Code examples
- [Backend README](civic-backend/README.md) - Backend setup

## API Response Examples

### Successful Authentication Response
```json
{
  "access_token": "eyJhbGc...",
  "refresh_token": "eyJhbGc...",
  "token_type": "bearer",
  "expires_in": 3600
}
```

### Issue Creation Response
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "title": "Pothole on Main Street",
  "status": "open",
  "created_at": "2024-04-06T10:30:00Z",
  "latitude": 21.1458,
  "longitude": 79.0882
}
```

### Error Response
```json
{
  "detail": "Validation error",
  "errors": [
    {
      "field": "phone",
      "message": "Invalid phone format"
    }
  ]
}
```

## Next Steps

1. Import the collection in Postman
2. Set up environment variables
3. Follow the Testing Workflow section
4. Integrate into your development workflow
5. Add custom tests for your use cases

---

**Last Updated**: April 6, 2024  
**API Version**: v1  
**Collection Version**: 1.0
