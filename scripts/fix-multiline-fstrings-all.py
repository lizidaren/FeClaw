"""扫描所有 .py 文件，修复多行 f-string 以兼容 Python 3.11"""
import re, glob, os

files_fixed = 0
total = 0

for path in glob.glob('**/*.py', recursive=True):
    if '/venv/' in path or '/.git/' in path or '/__pycache__/' in path:
        continue
    # Skip scripts that use textbook processing
    if '/textbook_pipeline/' in path:
        continue
    
    total += 1
    with open(path, 'r') as f:
        content = f.read()
    
    original = content
    lines = content.split('\n')
    result = []
    fix_count = 0
    i = 0
    
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        
        # Check if this line starts a multi-line f-string
        # Pattern: yield f"...{... or f" at line end with open brace
        # or any expression with f"{json.dumps({..." spanning multiple lines
        if stripped.startswith(('yield f"',)) and 'json.dumps({' in stripped:
            # Check if it closes on same line (ends with )}\\n\\n" or closing paren)
            # Different variants: ), ensure_ascii=False)}\\n\\n"
            #                        )}\\n\\n"
            closes_on_line = (
                stripped.endswith(')\\n\\n"') or
                stripped.endswith(')}\\n\\n"')
            )
            if not closes_on_line:
                # Collect multi-line block
                block_lines = [line]
                opens = line.count('{')
                closes = line.count('}')
                j = i + 1
                while j < len(lines):
                    s = lines[j].strip()
                    block_lines.append(lines[j])
                    opens += s.count('{')
                    closes += s.count('}')
                    # Some close patterns
                    if s.endswith(')\\n\\n"') or s.endswith(')}\\n\\n"'):
                        break
                    j += 1
                
                full_block = '\n'.join(block_lines)
                
                # Extract json.dumps content using regex
                m = re.search(r'json\.dumps\((\{.*\})\s*,\s*(ensure_ascii[^)]*)\)', full_block, re.DOTALL)
                if not m:
                    m = re.search(r'json\.dumps\((\{.*\})\)', full_block, re.DOTALL)
                
                if m:
                    dict_str = m.group(1)
                    kwargs = m.group(2) if m.lastindex >= 2 else 'ensure_ascii=False'
                    
                    varname = f'_ml_fstr_{fix_count}'
                    indent = line[:len(line) - len(line.lstrip())]
                    
                    result.append(f'{indent}{varname} = json.dumps({dict_str}, {kwargs})')
                    
                    # Need to extract topics/payload from block (try to find what inner expression is)
                    # Simpler: just write yield with variable
                    yield_line = re.sub(r'json\.dumps\(\{.*?\}\)', varname, full_block, count=1)
                    # Further simplify
                    result.append(f'{indent}yield f"event: pipeline\\ndata: {{{varname}}}\\n\\n"')
                    
                    fix_count += 1
                    i = j + 1 if j < len(lines) else len(lines)
                    continue
        
        result.append(line)
        i += 1
    
    new_content = '\n'.join(result)
    if new_content != original:
        with open(path, 'w') as f:
            f.write(new_content)
        files_fixed += 1
        print(f'  Fixed {fix_count} block(s) in {path}')

print(f'\nScanned {total} files, fixed {files_fixed} files')
