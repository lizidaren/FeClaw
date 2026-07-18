"""将 web_channel_service.py 中的所有多行 f-string 替换为变量预提取形式"""
import re

with open('services/web_channel_service.py', 'r') as f:
    content = f.read()

# Strategy: find each `yield f"event: pipeline\ndata: {json.dumps({`
# followed by multi-line dict, ending with `}, ensure_ascii=False)}\n\n"`
# Replace with extracted variable + yield using that variable

count = 0
result = []
i = 0
lines = content.split('\n')

while i < len(lines):
    line = lines[i]
    # Check if this line starts a multi-line f-string yield
    stripped = line.strip()
    
    # Pattern: line has yield f" containing json.dumps({ and doesn't close on same line
    if 'yield f"' in stripped and 'json.dumps({' in stripped:
        # Check if it closes on this line
        close_match = re.search(r'\)}\\n\\n"\)?$', stripped)
        if not close_match:
            # Multi-line block - find where it ends
            block_lines = [line]
            opens = line.count('{') - line.count('}')
            j = i + 1
            while j < len(lines) and opens > 0:
                block_lines.append(lines[j])
                opens += lines[j].count('{') - lines[j].count('}')
                j += 1
            
            full_block = '\n'.join(block_lines)
            
            # Extract the json.dumps content
            dumps_match = re.search(r'json\.dumps\((\{.*\})\s*,\s*(ensure_ascii[^)]*)\)', full_block, re.DOTALL)
            if dumps_match:
                dict_str = dumps_match.group(1)
                kwargs = dumps_match.group(2)
                
                varname = f'_pipe_data_{count}'
                indent = line[:len(line) - len(line.lstrip())]
                
                # Write variable extraction
                result.append(f'{indent}{varname} = json.dumps({dict_str}, {kwargs})')
                # Write yield with variable reference
                result.append(f'{indent}yield f"event: pipeline\\ndata: {{{varname}}}\\n\\n"')
                
                count += 1
                i = j
                continue
    
    result.append(line)
    i += 1

with open('services/web_channel_service.py', 'w') as f:
    f.write('\n'.join(result))

print(f'Fixed {count} multi-line f-strings')
