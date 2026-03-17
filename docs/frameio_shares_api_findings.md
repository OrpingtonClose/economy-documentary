# Frame.io Shares API - POST /v4/accounts/:account_id/projects/:project_id/shares

## Endpoint Information
- **Method**: POST
- **URL**: `https://api.frame.io/v4/accounts/:account_id/projects/:project_id/shares`
- **Purpose**: Create share
- **Rate Limits**: 10 calls per 1.00 minute(s) per account_user

## Request Body Schema

### data Object (Required)
The request body contains a `data` object with the following schema:

### Type Discriminator Field
- **Field Name**: `type`
- **Required**: Yes
- **Valid Values**: `"asset"` (only one variant found in documentation)

### Complete Schema for "asset" Type

```json
{
  "data": {
    "type": "asset",           // Required - discriminator field
    "access": "public",        // Required - enum: ["public", "secure"]
    "name": "Share Name",      // Required - string, 1-175 characters
    "asset_ids": [             // Optional - list of strings
      "96a645fb-3cdb-478d-bb65-60a4fb1e3ae8",
      "6d69958f-21f2-47f8-b3ea-fcef4d29f822"
    ],
    "description": "Description of share",  // Optional - string or null, <=640 characters
    "downloading_enabled": true,           // Optional - boolean
    "expiration": "2026-03-11T16:08:01.705378+00:00",  // Optional - string or null, format: "date-time"
    "passphrase": "as!dfj39sd(*"          // Optional - string or null, <=255 characters
  }
}
```

## Field Details

1. **type** (Required)
   - Type: string with value `"asset"`
   - This is the discriminator field
   - Documentation shows "1 variants" indicating only one type is supported

2. **access** (Required)
   - Type: enum
   - Allowed values: `"public"`, `"secure"`

3. **name** (Required)
   - Type: string
   - Length: 1-175 characters
   - Must include at least one non-whitespace character and no line breaks

4. **asset_ids** (Optional)
   - Type: list of strings
   - Description: Asset IDs (File, folder, and/or version stack IDs)

5. **description** (Optional)
   - Type: string or null
   - Length: <=640 characters
   - Note: Requires feature custom_branded_shares

6. **downloading_enabled** (Optional)
   - Type: boolean

7. **expiration** (Optional)
   - Type: string or null
   - Format: "date-time"
   - Description: Expiration timestamp

8. **passphrase** (Optional)
   - Type: string or null
   - Length: <=255 characters
   - Description: Passphrase to access share, if passphrase is required and not given it will be generated

## Response
- **Status Code**: 201 Created
- Returns the created share object with fields like:
  - `access`, `collection_id`, `commenting_enabled`, `created_at`, `description`, 
  - `downloading_enabled`, `enabled`, `expiration`, `id`, `last_viewed_at`, 
  - `name`, `short_url`, `updated_at`, `passphrase`

## Important Notes
- The documentation indicates only **ONE variant** exists for the data object
- The type field must be set to `"asset"` as this is the only supported type value
- The "1 variants" label in the API documentation confirms there is only one schema variant for creating shares
