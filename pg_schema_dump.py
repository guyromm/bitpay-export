#!/usr/bin/env python3
import os
import sys
import re
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Ephemeral objects to keep out of version control (none in this project); never-matching regex
# (an empty pattern would match every name)
SKIP_PATTERNS = re.compile(r'(?!x)x')

def generate_filename(content):
    """Extract Name and Type from content and generate filename."""
    match = re.search(r'Name: ([^;]+); Type: ([^;]+)', content)
    if not match:
        return None

    name, type_ = match.groups()

    # Apply transformations
    filename = f"{name}.{type_}"
    filename = re.sub(r'(Name: |Type: |; |\(|\)| )', '.', filename)
    filename = re.sub(r'\.+', '.', filename)
    filename = re.sub(r'^\.', '', filename)
    filename = re.sub(r'character\.varying[^.]*\.', 'cv.', filename)
    filename = re.sub(r'timestamp.with.time.zone', 'tstz', filename)
    filename = re.sub(r'"', '', filename)
    filename = filename + '.sql'

    # Truncate filenames that exceed filesystem limits (255 bytes)
    if len(filename.encode('utf-8')) > 250:
        import hashlib
        h = hashlib.md5(filename.encode()).hexdigest()[:8]
        # Keep the type suffix and a truncated name
        parts = filename.rsplit('.', 2)  # name.TYPE.sql
        if len(parts) >= 3:
            truncated = parts[0][:100] + '___' + h + '.' + parts[-2] + '.sql'
        else:
            truncated = filename[:200] + '___' + h + '.sql'
        filename = truncated

    return filename

def process_chunk(idx, content, jwt_secret):
    """Process a single chunk: generate filename and apply JWT replacement."""
    filename = generate_filename(content)
    if not filename:
        return None

    # Apply JWT secret replacement (guarded: an empty secret would rewrite every '' literal)
    processed_content = content
    if jwt_secret:
        processed_content = re.sub(
            f" '{re.escape(jwt_secret)}'",
            " current_setting('app.jwt_secret')",
            content
        )

    return (idx, filename, processed_content)

def main():
    # Setup paths
    dir_path = Path(__file__).parent.absolute()
    env_file = dir_path / '.env'
    schema_fn = dir_path / 'sql' / 'schema.sql'
    schema_dir = dir_path / 'sql' / 'schema'

    # Load environment
    env = {}
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                env[key] = value.strip('"\'')

    dburi = env.get('DBURI')
    jwt_secret = env.get('JWTSECRET', '')

    if not dburi:
        print("DBURI not found in .env", file=sys.stderr)
        sys.exit(1)

    print(f"[{subprocess.check_output(['date', '+%s.%N']).decode().strip()}] Starting pg_dump")

    # Run pg_dump
    schema_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ['pg_dump', '--schema-only', '--no-owner', dburi],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        print(f"pg_dump failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    dump_content = result.stdout

    # Filter out PostgreSQL administrative commands that add entropy
    # Use line-by-line filtering to preserve exact structure (like grep -v)
    filtered_lines = []
    for line in dump_content.splitlines(keepends=True):
        if not (line.startswith('\\connect') or
                line.startswith('\\restrict') or
                line.startswith('\\unrestrict')):
            filtered_lines.append(line)
    dump_content = ''.join(filtered_lines)

    print(f"[{subprocess.check_output(['date', '+%s.%N']).decode().strip()}] Finished pg_dump, splitting in memory")

    # Split by pattern in memory
    chunks = re.split(r'(?=^-- Name: )', dump_content, flags=re.MULTILINE)

    # Filter out empty chunks
    chunks = [c for c in chunks if c.strip()]

    print(f"[{subprocess.check_output(['date', '+%s.%N']).decode().strip()}] Split into {len(chunks)} chunks, processing in parallel")

    # Clean existing files (preserve .gitignore and subdirectories)
    for f in schema_dir.glob('*'):
        if f.name == '.gitignore' or f.is_dir():
            continue
        f.unlink()

    # Separate first chunk (header without "-- Name:") from named chunks
    first_chunk = None
    named_chunks = []

    skipped = 0
    partition_chunks = []
    for chunk in chunks:
        if '-- Name:' not in chunk:
            first_chunk = chunk
        else:
            name_match = re.search(r'-- Name: ([^;]+);', chunk)
            if name_match and SKIP_PATTERNS.search(name_match.group(1)):
                skipped += 1
                partition_chunks.append(chunk)
                continue
            named_chunks.append(chunk)

    if skipped:
        print(f"  {skipped} ephemeral shard objects -> partitions/")

    # Process named chunks in parallel
    results = {}
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as executor:
        futures = {
            executor.submit(process_chunk, idx, chunk, jwt_secret): idx
            for idx, chunk in enumerate(named_chunks)
        }

        for future in as_completed(futures):
            result = future.result()
            if result:
                idx, filename, content = result
                results[idx] = (filename, content)

    print(f"[{subprocess.check_output(['date', '+%s.%N']).decode().strip()}] Finished processing, writing files")

    # Write first chunk as file '00' if it exists
    if first_chunk:
        (schema_dir / '00').write_text(first_chunk)

    # Write partition objects to partitions/ subdirectory
    if partition_chunks:
        part_dir = schema_dir / 'partitions'
        if part_dir.exists():
            for f in part_dir.glob('*.sql'):
                f.unlink()
        else:
            part_dir.mkdir()
        for chunk in partition_chunks:
            fn = generate_filename(chunk)
            if fn:
                content = chunk
                if jwt_secret:
                    content = re.sub(f" '{re.escape(jwt_secret)}'",
                                     " current_setting('app.jwt_secret')", content)
                (part_dir / fn).write_text(content)

    # Write files in original order
    order_list = []
    for idx in sorted(results.keys()):
        filename, content = results[idx]
        file_path = schema_dir / filename
        file_path.write_text(content)
        order_list.append(filename)

    # Write order.txt
    order_file = schema_dir / 'order.txt'
    order_file.write_text('\n'.join(order_list) + '\n')

    print(f"[{subprocess.check_output(['date', '+%s.%N']).decode().strip()}] Finished writing files, checking uniqueness")

    # Check uniqueness
    if len(order_list) != len(set(order_list)):
        print("FILENAMES NOT UNIQUE!", file=sys.stderr)
        sys.exit(1)

    # Reconstruct and validate
    print(f"[{subprocess.check_output(['date', '+%s.%N']).decode().strip()}] Starting reconstruction")

    os.chdir(schema_dir)
    reconstructed = '00\n' + '\n'.join(order_list)
    files_to_cat = reconstructed.split('\n')

    reconstructed_content = []
    for fn in files_to_cat:
        fn = fn.strip()
        if fn and (schema_dir / fn).exists():
            reconstructed_content.append((schema_dir / fn).read_text())

    reconstructed_schema = ''.join(reconstructed_content)

    # Filter original dump to match — remove skipped partition chunks
    filtered_chunks = []
    for chunk in re.split(r'(?=^-- Name: )', dump_content, flags=re.MULTILINE):
        name_match = re.search(r'-- Name: ([^;]+);', chunk)
        if name_match and SKIP_PATTERNS.search(name_match.group(1)):
            continue
        filtered_chunks.append(chunk)
    filtered_dump = ''.join(filtered_chunks)

    print(f"[{subprocess.check_output(['date', '+%s.%N']).decode().strip()}] Finished reconstruction, starting diff")

    # Write temp files for diff
    orig_tmp = schema_dir / '.schema.orig.tmp'
    recon_tmp = schema_dir / '.schema.recon.tmp'
    orig_tmp.write_text(filtered_dump)
    recon_tmp.write_text(reconstructed_schema)

    # Run diff
    diff_result = subprocess.run(
        ['diff', '-Nuar', str(orig_tmp), str(recon_tmp)],
        capture_output=True
    )

    if diff_result.returncode != 0:
        print(f"DUMPS NOT EQUAL!", file=sys.stderr)
        print(diff_result.stdout.decode(), file=sys.stderr)
        sys.exit(2)

    # Clean up temp files
    orig_tmp.unlink()
    recon_tmp.unlink()

    print(f"[{subprocess.check_output(['date', '+%s.%N']).decode().strip()}] Diff passed, rewriting view creation")

    # Rewrite CREATE VIEW to CREATE OR REPLACE VIEW
    for sql_file in schema_dir.glob('*.sql'):
        content = sql_file.read_text()
        content = re.sub(r'^CREATE (VIEW|FUNCTION)', 'CREATE OR REPLACE \\1', content, flags=re.MULTILINE)
        sql_file.write_text(content)

    print(f"[{subprocess.check_output(['date', '+%s.%N']).decode().strip()}] All done!")

if __name__ == '__main__':
    main()
