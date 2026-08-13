ALTER TABLE bar_restock_request_items
ADD COLUMN IF NOT EXISTS approved_quantity INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'bar_restock_request_items_approved_quantity_check'
          AND conrelid = 'bar_restock_request_items'::regclass
    ) THEN
        ALTER TABLE bar_restock_request_items
        ADD CONSTRAINT bar_restock_request_items_approved_quantity_check
        CHECK (
            approved_quantity IS NULL
            OR (approved_quantity > 0 AND approved_quantity <= quantity)
        );
    END IF;
END $$;
