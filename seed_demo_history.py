"""
Demo-prep script -- NOT part of the running app. Run this once, a few
minutes before presenting, to pre-load one realistic "old" conversation
for a demo farmer, so real multi-month recall can be shown live instead
of requiring judges to wait actual months.

Usage:
    export $(cat .env | xargs)
    python3 seed_demo_history.py 9999999999

Then, live in the demo, use that same phone number and ask:
  "My wheat leaves are turning yellow with insects again"
(deliberately close wording to what's seeded -- reliable regardless of
embedding quality)
"""
import sys
from datetime import datetime, timedelta
from farmer_memory import FarmerMemoryModule, SQLiteStorage

farmer_id = sys.argv[1] if len(sys.argv) > 1 else "9999999999"
memory = FarmerMemoryModule(storage=SQLiteStorage("kissan_saathi.db"))

memory.log_interaction(
    farmer_id=farmer_id,
    query_text="My wheat leaves are turning yellow and I see small insects on them",
    response_text="This sounds like an aphid infestation. Spray Imidacloprid 17.8% SL at 0.5ml per litre of water, in the evening for best results.",
    crop="Wheat",
    intent="pest_query",
    timestamp=datetime.utcnow() - timedelta(days=92),
)

print(f"Seeded one 3-month-old interaction for farmer_id={farmer_id}.")
print('Live in the demo, ask: "My wheat leaves are turning yellow with insects again"')
