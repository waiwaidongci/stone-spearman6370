# Merge session lifecycle states, shared by services and API.
MERGE_STATUS=(
    'awaiting',      # only one revision package received
    'conflicts',     # three-way computed, conflicts wait for adjudication
    'resolved',      # all conflicts adjudicated, ready for atomic apply
    'applied',       # official observations replaced atomically
    'superseded',    # replaced by a newer session of the same study
)
# Conflict kinds produced by the three-way engine.
CONFLICT_KINDS=(
    'same_item',         # both sides changed the same row differently
    'delete_modify',     # one side withdrew the row, the other modified it
    'key_occupation',    # a (re)numbered key collides with a live row
    'add_collision',     # both sides added rows carrying the same source key
)
SIDE_LABELS={'A': '甲实验室', 'B': '乙实验室'}
