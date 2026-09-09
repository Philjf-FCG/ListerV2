import sqlite3

conn = sqlite3.connect('backend/lister.db')

# List all tables
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print("Tables:", [t[0] for t in tables])

# Check vinted_items columns
if 'vinted_items' in [t[0] for t in tables]:
    cols = [desc[0] for desc in conn.execute('PRAGMA table_info(vinted_items)').fetchall()]
    print("\nvinted_items columns:", cols)
    
    # Show sample data
    sample = conn.execute("SELECT * FROM vinted_items LIMIT 2").fetchall()
    if sample:
        print(f"\nFound {len(sample)} rows")
        for row in sample:
            print(row)
else:
    print("\nvinted_items table not found")

conn.close()
