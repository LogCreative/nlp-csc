import argparse

def main():
    parser = argparse.ArgumentParser(description='Generate SFT data')
    parser.add_argument('--input', type=str, required=True, help='Input file')
    parser.add_argument('--output', type=str, required=True, help='Output file')
    args = parser.parse_args()

    input_file = args.input
    output_file = args.output

    with open(input_file, "r", encoding="utf-8") as f:
        lines = []
        first_line = f.readline()   # ignore the first line
        for line in f:
            cols = line.replace('"','\\"').strip().split("\t")      # " is a special character for jsonl
            if len(cols) == 3:
                lines.append((cols[0], cols[1].split(), cols[2].split()))
            else:  # it is a test set
                lines.append((cols[0], cols[1].split(), []))
    
    with open(output_file, "w", encoding="utf-8") as f:
        for line in lines:
            f.write('{"id":"%s","instruction":"修改句子中的错别字","input":"%s","response": "%s"}\n' % (line[0], ''.join(line[1]), ''.join(line[2])))

if __name__ == "__main__":
    main()