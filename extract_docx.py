import docx
import sys

def read_docx(file_path):
    try:
        doc = docx.Document(file_path)
        full_text = []
        for para in doc.paragraphs:
            if para.text.strip():
                full_text.append(para.text)
        return '\n'.join(full_text)
    except Exception as e:
        return f"Error: {str(e)}"

if __name__ == "__main__":
    content = read_docx('data/输入.docx')
    with open('input_content.txt', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Content extracted to input_content.txt")
