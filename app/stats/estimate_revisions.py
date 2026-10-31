"""Transactional invalidation of derived estimates; no reading data is stored here."""


def triggers():
    statements = []
    def trigger(name, table, operation, body, columns=''):
        clause = f' OF {columns}' if columns and operation == 'UPDATE' else ''
        statements.append(f'CREATE TRIGGER IF NOT EXISTS eta_{name}_{operation} '
                          f'AFTER {operation}{clause} ON {table} BEGIN {body} END;')

    def account(expression):
        return f'''INSERT INTO estimate_account_revisions(user_id,revision)
            SELECT {expression},1 WHERE EXISTS(SELECT 1 FROM users WHERE id={expression})
            ON CONFLICT(user_id) DO UPDATE SET revision=revision+1;'''

    def work(user, kind, identity):
        return f'''INSERT INTO estimate_work_revisions(user_id,kind,identity,revision)
            SELECT {user},'{kind}',{identity},1
            WHERE {identity} IS NOT NULL AND EXISTS(SELECT 1 FROM users WHERE id={user})
            ON CONFLICT(user_id,kind,identity) DO UPDATE SET revision=revision+1;'''

    global_change = 'UPDATE estimate_revision SET revision=revision+1 WHERE id=1;'
    source_tables = {
        'items': 'id,series_id,file_path,file_hash,file_size,text_size,page_count,file_format',
        'series': 'library_id,folder_path',
        'chapters': 'id,novel_id,volume_id,content_html,content_size,discovered_at,order_index',
        'volumes': 'novel_id',
        'novels': 'id',
        'removed_items': 'library_id,folder_path,page_count,file_format',
    }
    for table, columns in source_tables.items():
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            trigger(table, table, operation, global_change, columns)
    for table in ('account_pace_totals', 'item_pace_totals', 'novel_pace_totals',
                  'item_read_totals', 'removed_item_reads'):
        for operation, refs in (('INSERT', ('NEW',)), ('UPDATE', ('OLD', 'NEW')), ('DELETE', ('OLD',))):
            trigger(table, table, operation, ''.join(account(f'{ref}.user_id') for ref in refs))
    for operation, refs in (('INSERT', ('NEW',)), ('UPDATE', ('OLD', 'NEW')), ('DELETE', ('OLD',))):
        body = ''
        for ref in refs:
            item = f'(SELECT series_id FROM items WHERE id={ref}.item_id)'
            body += work(f'{ref}.user_id', 'series', item)
            # Comic progress is rate evidence for its series AND the fallback.
            body += f'''INSERT INTO estimate_account_revisions(user_id,revision)
                SELECT {ref}.user_id,1 FROM items WHERE id={ref}.item_id AND file_format='cbz'
                AND EXISTS(SELECT 1 FROM users WHERE id={ref}.user_id)
                ON CONFLICT(user_id) DO UPDATE SET revision=revision+1;'''
        trigger('progress', 'reading_progress', operation, body)
        for table, identity in (('novel_reading_progress', f'{ref}.novel_id'),
                                ('novel_read_chapters', f'(SELECT novel_id FROM chapters WHERE id={ref}.chapter_id)')):
            body = ''.join(work(f'{ref}.user_id', 'novel',
                           f'{ref}.novel_id' if table == 'novel_reading_progress' else
                           f'(SELECT novel_id FROM chapters WHERE id={ref}.chapter_id)') for ref in refs)
            trigger(table, table, operation, body)
        body = ''.join(work(f'{ref}.user_id', 'series', f'{ref}.series_id') +
                       work(f'{ref}.user_id', 'novel', f'{ref}.novel_id') for ref in refs)
        trigger('rules', 'text_replacements', operation, body)
    for table, kind in (('series', 'series'), ('novels', 'novel')):
        trigger('forget_' + kind, table, 'DELETE',
                f"DELETE FROM estimate_work_revisions WHERE kind='{kind}' AND identity=OLD.id;")
    trigger('account', 'users', 'UPDATE', account('NEW.id'), 'password_hash,created_at')
    trigger('account', 'users', 'DELETE', global_change)
    return '\n'.join(statements)
