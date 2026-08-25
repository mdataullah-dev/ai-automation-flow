import os
import re
import sqlite3

import pandas as pd
from thefuzz import fuzz

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'data')

# ==========================================
# 1. CLEANING FUNCTIONS
# ==========================================
def clean_phone(phone):
    if pd.isna(phone):
        return None
    # Remove everything except digits
    digits = re.sub(r'\D', '', str(phone))
    # If it starts with 91 and is 12 digits, strip the 91
    if len(digits) > 10 and digits.startswith('91'):
        digits = digits[2:]
    return digits[-10:] if len(digits) >= 10 else None

def clean_email(email):
    if pd.isna(email) or not isinstance(email, str):
        return None
    return email.strip().lower()

def clean_name(name):
    if pd.isna(name):
        return ""
    return str(name).strip().title()

def clean_boolean(val):
    if pd.isna(val):
        return False
    val = str(val).strip().lower()
    return val in ['y', 'yes', 'true', '1']

# ==========================================
# 2. INGESTION & NORMALIZATION
# ==========================================
def ingest_data():
    print("Ingesting and cleaning raw data...")
    
    # SOURCE 1: Naukri
    df1 = pd.read_csv(os.path.join(DATA_DIR, 'source1_naukri_applicants.csv'))
    df1['email'] = df1['Email'].apply(clean_email)
    df1['phone'] = df1['Phone'].apply(clean_phone)
    df1['name'] = df1['Full Name'].apply(clean_name)
    df1['city'] = df1['City'].apply(lambda x: str(x).title() if pd.notna(x) else None)
    df1['skills'] = df1['Skills']
    df1['source'] = 'Naukri'
    df1_clean = df1[['name', 'email', 'phone', 'city', 'skills', 'source']].dropna(subset=['name'])

    # SOURCE 2: Gig Workers
    df2 = pd.read_csv(os.path.join(DATA_DIR, 'source2_gig_workers.csv'))
    # here we are fixing the planted bug: Handle shifted rows (e.g., email in worker_name column)
    def fix_shifted_row(row):
        if pd.notna(row['email_id']) and '@' not in str(row['email_id']) and '@' in str(row['worker_name']):
            # Row is shifted!
            return pd.Series([row['worker_name'], '', row['email_id']]) # email, name (missing), skills (shifted)
        return pd.Series([row['email_id'], row['worker_name'], row['skill_tags']])
    
    fixed_cols = df2.apply(fix_shifted_row, axis=1)
    df2['email'] = fixed_cols[0].apply(clean_email)
    df2['name'] = fixed_cols[1].apply(clean_name)
    df2['skills'] = fixed_cols[2]
    df2['phone'] = None # Gig workers don't have phones in this CSV
    df2['city'] = df2['location'].apply(lambda x: str(x).title() if pd.notna(x) else None)
    df2['source'] = 'GigWorkers'
    df2_clean = df2[['name', 'email', 'phone', 'city', 'skills', 'source']].dropna(subset=['email']) # Need email at least

    # SOURCE 3: CBNexus
    df3 = pd.read_csv(os.path.join(DATA_DIR, 'source3_cbnexus_contacts.csv'))
    # here we are fixing the planted bug: Remove repeated header rows inside data
    df3 = df3[df3['Name'] != 'Name'] 
    
    df3['name'] = df3['Name'].apply(clean_name)
    df3['phone'] = df3['Phone Number'].apply(clean_phone)
    df3['email'] = None # No email in this CSV
    df3['city'] = df3['City'].apply(lambda x: str(x).title() if pd.notna(x) else None)
    df3['verified'] = df3['Verified'].apply(clean_boolean)
    df3['source'] = 'CBNexus'
    df3_clean = df3[['name', 'email', 'phone', 'city', 'source']]

    # Combine all into one massive dataframe for processing
    return pd.concat([df1_clean, df2_clean, df3_clean], ignore_index=True)

# ==========================================
# 3. ENTITY RESOLUTION (THE MERGE)
# ==========================================
def resolve_entities(combined_df):
    print("Resolving entities and merging duplicates...")
    golden_records = []
    
    for _, row in combined_df.iterrows():
        match_found = False
        
        for record in golden_records:
            # Match condition 1: Exact Email
            if pd.notna(row['email']) and row['email'] == record['email']:  # noqa: SIM114
                match_found = True
            # Match condition 2: Exact Phone
            elif pd.notna(row['phone']) and row['phone'] == record['phone']:
                match_found = True
            # Match condition 3: Fuzzy Name match (if both lack strict identifiers but names are highly similar)
            elif fuzz.ratio(row['name'], record['name']) > 85:  # noqa: SIM102
                # Extra safety: Ensure they are in the same city if we are just guessing by name
                if pd.notna(row['city']) and pd.notna(record['city']) and row['city'] == record['city']:
                    match_found = True

            if match_found:
                # Merge data (keep the longest/most complete string)
                record['name'] = row['name'] if len(row['name']) > len(record.get('name', '')) else record['name']
                record['email'] = row['email'] if pd.notna(row['email']) else record['email']
                record['phone'] = row['phone'] if pd.notna(row['phone']) else record['phone']
                record['city'] = row['city'] if pd.notna(row['city']) else record['city']
                
                # Append skills
                if pd.notna(row.get('skills')):
                    existing_skills = record.get('skills', '')
                    if not existing_skills:
                        record['skills'] = row['skills']
                    elif row['skills'] not in existing_skills:
                        record['skills'] = f"{existing_skills}, {row['skills']}"
                
                # Append sources
                if row['source'] not in record['sources']:
                    record['sources'].append(row['source'])
                break
                
        if not match_found:
            golden_records.append({
                'name': row['name'],
                'email': row['email'],
                'phone': row['phone'],
                'city': row['city'],
                'skills': row.get('skills', ''),
                'sources': [row['source']]
            })
            
    # Convert sources list to string for SQL
    for r in golden_records:
        r['sources'] = ", ".join(r['sources'])
        
    return pd.DataFrame(golden_records)


# ==========================================
# 4. DATABASE EXPORT
# ==========================================
def save_to_db(final_df):
    print("Saving clean golden records to SQLite database...")
    
    # Create db folder dynamically
    DB_DIR = os.path.join(BASE_DIR, 'db')
    os.makedirs(DB_DIR, exist_ok=True)
    db_path = os.path.join(DB_DIR, 'consultbae.sqlite3')
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Create the unified table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT UNIQUE,
            phone TEXT UNIQUE,
            city TEXT,
            skills TEXT,
            sources TEXT
        )
    ''')
    
    # Insert data
    final_df.to_sql('users', conn, if_exists='replace', index=False)
    
    # Validate
    cursor.execute("SELECT COUNT(*) FROM users")
    count = cursor.fetchone()[0]
    print(f"Success! {count} unique user records saved to {db_path}")
    conn.close()

if __name__ == "__main__":
    raw_data = ingest_data()
    clean_data = resolve_entities(raw_data)
    save_to_db(clean_data)