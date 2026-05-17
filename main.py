from rst_reader import read_rst

with read_rst(r"file.rst") as rst:
    print(rst)                          # RSTReader('file.rst', nodes=12480, ...)
    print(rst.summary())                # table of load steps

    nnum, disp = rst.nodal_solution(0) # UX, UY, UZ per node
    nnum, stress = rst.nodal_stress(0) # SX SY SZ SXY SYZ SXZ
    ps = rst.principal_stress(stress)  # S1 S2 S3 SINT SEQV